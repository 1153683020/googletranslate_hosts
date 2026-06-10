#!/usr/bin/env python3
import argparse
import concurrent.futures
import datetime as dt
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DOMAINS = [
    "translate.google.com",
    "translate.googleapis.com",
    "translate-pa.googleapis.com",
    "translation.googleapis.com"
]

DOH_ENDPOINTS = [
    "https://dns.google/resolve",
    "https://cloudflare-dns.com/dns-query",
    "https://dns.quad9.net/dns-query",
]

GT_PORTS = [80,443]
GLOBALPING_API = "https://api.globalping.io/v1/measurements"


@dataclass(frozen=True)
class ProbeResult:
    ip: str
    port: int
    ok: bool
    latency_ms: float
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve and probe Google translate hosts, then generate a hosts subscription."
    )
    parser.add_argument("--domains-file", type=Path, default=Path("domains.txt"))
    parser.add_argument("--output", type=Path, default=Path("dist/hosts.txt"))
    parser.add_argument("--json-output", type=Path, default=Path("dist/result.json"))
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=48)
    parser.add_argument("--max-ips-per-domain", type=int, default=1)
    parser.add_argument(
        "--probe-provider",
        choices=["globalping", "local"],
        default="globalping",
        help="Use Globalping China probes first, or only probe from the current runner.",
    )
    parser.add_argument("--globalping-location", default="China")
    parser.add_argument("--globalping-limit", type=int, default=1)
    parser.add_argument("--globalping-packets", type=int, default=3)
    parser.add_argument("--globalping-timeout", type=float, default=45.0)
    parser.add_argument(
        "--fallback-to-dns",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use DNS candidates if every active probe fails.",
    )
    parser.add_argument(
        "--custom-ips-file",
        type=Path,
        default=Path("custom_ips.txt"),
        help="Optional extra IPv4 candidates, one per line.",
    )
    return parser.parse_args()


def load_lines(path: Path, fallback: list[str]) -> list[str]:
    if not path.exists():
        return fallback
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines or fallback


def valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def resolve_system(domain: str) -> set[str]:
    ips: set[str] = set()
    try:
        for item in socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = item[4][0]
            if valid_ipv4(ip):
                ips.add(ip)
    except OSError:
        pass
    return ips


def resolve_doh(domain: str, endpoint: str, timeout: float) -> set[str]:
    query = urllib.parse.urlencode({"name": domain, "type": "A"})
    url = f"{endpoint}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "accept": "application/dns-json",
            "user-agent": "gtranslate-hosts-generator/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return set()

    ips: set[str] = set()
    for answer in data.get("Answer", []):
        ip = str(answer.get("data", "")).strip()
        if answer.get("type") == 1 and valid_ipv4(ip):
            ips.add(ip)
    return ips


def resolve_domain(domain: str, timeout: float) -> set[str]:
    ips = resolve_system(domain)
    for endpoint in DOH_ENDPOINTS:
        ips.update(resolve_doh(domain, endpoint, timeout))
    return ips


def probe_tcp(ip: str, port: int, timeout: float) -> ProbeResult:
    start = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            latency_ms = (time.perf_counter() - start) * 1000
            return ProbeResult(ip=ip, port=port, ok=True, latency_ms=latency_ms)
    except OSError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return ProbeResult(ip=ip, port=port, ok=False, latency_ms=latency_ms, error=str(exc))


def probe_tls(ip: str, domain: str, timeout: float) -> ProbeResult:
    start = time.perf_counter()
    context = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=domain):
                latency_ms = (time.perf_counter() - start) * 1000
                return ProbeResult(ip=ip, port=443, ok=True, latency_ms=latency_ms)
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return ProbeResult(ip=ip, port=443, ok=False, latency_ms=latency_ms, error=str(exc))


def globalping_headers() -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "user-agent": "gtranslate-hosts-generator/1.0",
    }
    token = os.environ.get("GLOBALPING_TOKEN", "").strip()
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def globalping_request(method: str, url: str, payload: dict[str, object] | None, timeout: float) -> dict[str, object]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=globalping_headers())
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def globalping_extract_stats(result: dict[str, object]) -> tuple[bool, float, str]:
    stats = result.get("stats")
    if isinstance(stats, dict):
        loss = float(stats.get("loss", 100) or 0)
        received = int(stats.get("rcv", 0) or 0)
        avg = float(stats.get("avg", 999999.0) or 999999.0)
        return received > 0 and loss < 100, avg, ""

    raw = str(result.get("rawOutput", ""))
    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s+packet loss", raw)
    avg_match = re.search(r"=\s*[\d.]+/([\d.]+)/[\d.]+/[\d.]+\s*ms", raw)
    loss = float(loss_match.group(1)) if loss_match else 100.0
    avg = float(avg_match.group(1)) if avg_match else 999999.0
    return loss < 100, avg, ""


def probe_globalping_tcp(
    ip: str,
    port: int,
    location: str,
    limit: int,
    packets: int,
    timeout: float,
) -> list[dict[str, object]]:
    payload = {
        "type": "ping",
        "target": ip,
        "locations": [{"magic": location}],
        "limit": limit,
        "measurementOptions": {
            "packets": packets,
            "protocol": "TCP",
            "port": port,
        },
    }
    deadline = time.monotonic() + timeout
    created = globalping_request("POST", GLOBALPING_API, payload, timeout)
    measurement_id = str(created["id"])

    while time.monotonic() < deadline:
        time.sleep(2)
        measurement = globalping_request("GET", f"{GLOBALPING_API}/{measurement_id}", None, timeout)
        if measurement.get("status") not in {"in-progress", "queued"}:
            details: list[dict[str, object]] = []
            for item in measurement.get("results", []):
                if not isinstance(item, dict):
                    continue
                probe = item.get("probe", {})
                result = item.get("result", {})
                if not isinstance(result, dict):
                    continue
                ok, avg, error = globalping_extract_stats(result)
                details.append(
                    {
                        "provider": "globalping",
                        "measurement_id": measurement_id,
                        "ip": ip,
                        "port": port,
                        "ok": ok,
                        "latency_ms": avg,
                        "error": error or str(result.get("error", "")),
                        "location": {
                            "country": probe.get("country") if isinstance(probe, dict) else None,
                            "city": probe.get("city") if isinstance(probe, dict) else None,
                            "asn": probe.get("asn") if isinstance(probe, dict) else None,
                            "network": probe.get("network") if isinstance(probe, dict) else None,
                        },
                        "stats": result.get("stats"),
                    }
                )
            return details

    return [
        {
            "provider": "globalping",
            "ip": ip,
            "port": port,
            "ok": False,
            "latency_ms": 999999.0,
            "error": "globalping measurement timed out",
        }
    ]


def pick_ip_with_globalping(
    candidates: set[str],
    location: str,
    limit: int,
    packets: int,
    timeout: float,
    workers: int,
    max_ips: int,
) -> tuple[list[str], list[dict[str, object]]]:
    details: list[dict[str, object]] = []
    scores: dict[str, tuple[int, float]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, max(1, len(candidates)))) as executor:
        futures = [
            executor.submit(probe_globalping_tcp, ip, 5228, location, limit, packets, timeout)
            for ip in candidates
        ]
        for future in concurrent.futures.as_completed(futures):
            for result in future.result():
                details.append(result)
                ip = str(result["ip"])
                ok_count, latency = scores.get(ip, (0, 999999.0))
                if result.get("ok"):
                    scores[ip] = (ok_count + 1, min(latency, float(result["latency_ms"])))
                else:
                    scores.setdefault(ip, (ok_count, latency))

    ranked = sorted(scores.items(), key=lambda item: (-item[1][0], item[1][1], item[0]))
    picked = [ip for ip, (ok_count, _) in ranked if ok_count > 0][:max_ips]
    return picked, details


def pick_ip_for_domain(
    domain: str,
    candidates: set[str],
    timeout: float,
    workers: int,
    max_ips: int,
    fallback_to_dns: bool,
    probe_provider: str,
    globalping_location: str,
    globalping_limit: int,
    globalping_packets: int,
    globalping_timeout: float,
) -> tuple[list[str], list[dict[str, object]]]:
    if probe_provider == "globalping" and candidates:
        try:
            picked, details = pick_ip_with_globalping(
                candidates=candidates,
                location=globalping_location,
                limit=globalping_limit,
                packets=globalping_packets,
                timeout=globalping_timeout,
                workers=workers,
                max_ips=max_ips,
            )
            if picked or not fallback_to_dns:
                return picked, details
        except Exception as exc:
            print(f"{domain}: Globalping probe failed, fallback to local probe: {exc}", file=sys.stderr)

    probe_jobs: list[tuple[str, int]] = [(ip, port) for ip in candidates for port in GT_PORTS]
    results: list[ProbeResult] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(probe_tcp, ip, port, timeout) for ip, port in probe_jobs]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    scores: dict[str, tuple[int, float]] = {}
    details: list[dict[str, object]] = []
    for result in results:
        details.append(result.__dict__)
        ok_count, latency = scores.get(result.ip, (0, 999999.0))
        if result.ok:
            scores[result.ip] = (ok_count + 1, min(latency, result.latency_ms))
        else:
            scores.setdefault(result.ip, (ok_count, latency))

    ranked = sorted(scores.items(), key=lambda item: (-item[1][0], item[1][1], item[0]))
    picked = [ip for ip, (ok_count, _) in ranked if ok_count > 0][:max_ips]

    # Port probing is the main signal for mtalk. A successful TLS probe to 443 is a bonus
    # for Google frontends and helps filter DNS answers that are reachable but unsuitable.
    tls_checked: list[str] = []
    if picked:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(picked))) as executor:
            futures = [executor.submit(probe_tls, ip, domain, timeout) for ip in picked]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                details.append(result.__dict__ | {"tls": True})
                if result.ok:
                    tls_checked.append(result.ip)

    if not picked and fallback_to_dns:
        picked = sorted(candidates)[:max_ips]

    return tls_checked or picked, details


def render_hosts(domains: list[str], domain_ips: dict[str, list[str]]) -> str:
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# GT hosts subscription for BindHosts",
        f"# Generated at: {now}",
        "# Source: https://github.com/1153683020/gogtranslate_hosts/actions",
        "#",
        "# The first reachable IP for each domain is placed first.",
    ]
    for domain in domains:
        ips = domain_ips.get(domain, [])
        if not ips:
            lines.append(f"# No reachable IP found for {domain}")
            continue
        for ip in ips:
            lines.append(f"{ip} {domain}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    domains = load_lines(args.domains_file, DEFAULT_DOMAINS)
    custom_ips = set(load_lines(args.custom_ips_file, []))
    custom_ips = {ip for ip in custom_ips if valid_ipv4(ip)}

    all_results: dict[str, object] = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "domains": {},
    }
    domain_ips: dict[str, list[str]] = {}

    for domain in domains:
        candidates = resolve_domain(domain, args.timeout)
        candidates.update(custom_ips)
        picked, details = pick_ip_for_domain(
            domain=domain,
            candidates=candidates,
            timeout=args.timeout,
            workers=args.workers,
            max_ips=args.max_ips_per_domain,
            fallback_to_dns=args.fallback_to_dns,
            probe_provider=args.probe_provider,
            globalping_location=args.globalping_location,
            globalping_limit=args.globalping_limit,
            globalping_packets=args.globalping_packets,
            globalping_timeout=args.globalping_timeout,
        )
        domain_ips[domain] = picked
        all_results["domains"][domain] = {
            "candidates": sorted(candidates),
            "picked": picked,
            "probe_results": details,
        }
        print(f"{domain}: {len(candidates)} candidates, {len(picked)} picked", file=sys.stderr)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_hosts(domains, domain_ips), encoding="utf-8", newline="\n")
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
