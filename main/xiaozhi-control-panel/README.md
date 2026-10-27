# 小智本机控制台

这是一个独立的、本机监听的控制台，提供：

- 网易云二维码绑定、解绑和绑定状态；不会向浏览器返回 Cookie。
- 网易云音乐配置的白名单编辑；保存前会备份 `data/.config.yaml`。
- `xiaozhi-server` 与 `ncm-api` 的状态和安全重启。

它默认只监听 `127.0.0.1:8090`，不包含登录功能。请通过 SSH 隧道使用，而不要直接将端口公开到互联网：

```powershell
ssh -i D:\wx\aliyun_ssh.pem -N -L 8090:127.0.0.1:8090 root@YOUR_SERVER_IP
```

随后在本机浏览器打开 `http://127.0.0.1:8090`。

## 部署

在服务器仓库更新后，将 `xiaozhi-control-panel.service` 复制到
`/etc/systemd/system/`，然后执行：

```bash
systemctl daemon-reload
systemctl enable --now xiaozhi-control-panel
systemctl status xiaozhi-control-panel
```

这个服务以 root 运行，是为了安全地写入仅 root 可读的网易云环境文件并重启固定服务。不要将控制台监听地址改为公网地址；如果未来要从固定公网 IP 访问，应由 Nginx 和安全组共同实施 IP 白名单。
