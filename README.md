# 谷歌翻译 Hosts（自用）

定时解析并探测 谷歌翻译 域名，生成给 BindHosts 使用的 hosts 订阅。

## 订阅地址

启用 GitHub Pages 后，订阅地址通常是：

```text
https://<your-name>.github.io/<repo-name>/hosts.txt
```

也可以直接使用仓库里的 raw 文件：

```text
https://raw.githubusercontent.com/<your-name>/<repo-name>/main/dist/hosts.txt
```

jsdelivr

```text
https://cdn.jsdelivr.net/gh/1153683020/googletranslate_hosts/dist/hosts.txt
```
**最近一次更改**

~实验性修改：添加了来自项目[GoogleTranslateIpCheck](https://github.com/Ponderfly/GoogleTranslateIpCheck)的ip源~，在此感谢Ponderfly大佬的项目

该修改已经移除，原因是影响Globalping的结果

## 使用方式

1. 把这些文件推送到 GitHub 仓库。
2. 进入仓库 `Settings` -> `Pages`，把 Source 设为 `GitHub Actions`。
3. 到 `Actions` 手动运行一次 `Update gtranslate hosts`。
4. 在 BindHosts 里添加生成的 `hosts.txt` 订阅地址。

工作流默认每 6 小时运行一次，也可以在 `.github/workflows/update-hosts.yml` 修改 cron。

## 自定义

- `domains.txt`：要生成 hosts 的域名列表。
- `custom_ips.txt`：手工补充候选 IPv4，每行一个。脚本会把这些 IP 和 DNS 结果一起探测。
- `dist/hosts.txt`：BindHosts 订阅文件。默认每个域名输出 1 个 IP，避免不同 hosts 解析器处理重复域名时行为不一致。
- `dist/result.json`：每个域名的候选 IP、选择结果和探测详情。
- `GLOBALPING_TOKEN`：可选。仓库 Secret 里配置后会用于 Globalping API，提高额度；不配置也可以使用匿名额度。

## 说明

工作流默认用 Globalping 的 `China` 探针对候选 IP 执行 TCP 80/443 探测，并按国内探针返回的可用性和延迟排序。如果 Globalping 调用失败，脚本会回退到 GitHub runner 本地探测；如果所有主动探测都失败，会保留 DNS 候选，避免订阅被刷空。

国内探针位置和运营商不固定，结果只能代表当次探测节点。实际效果取决于运营商、地区和当前网络状态；如果你有本地实测可用 IP，建议放进 `custom_ips.txt`。

感谢原项目提供的代码和思路
