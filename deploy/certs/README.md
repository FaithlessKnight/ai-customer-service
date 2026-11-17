# HTTPS 证书

在部署环境中放入自己的 `fullchain.pem` 和 `privkey.pem`，并将 `deploy/nginx.conf` 中的域名改为实际域名，然后启动 Nginx。

证书与私钥由 `.gitignore` 排除，不随源代码上传。现有本地文件会保留。开发环境可直接访问本地 Uvicorn 的 8000 端口，无需配置 HTTPS。
