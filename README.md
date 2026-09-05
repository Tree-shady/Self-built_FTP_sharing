# Self-built FTP Sharing — 自建 FTP/SFTP 共享客户端 (Python + PySide6)

一个面向 **Windows** 场景的文件共享工具箱：

- **客户端**(PySide6 桌面程序)：FTPS/SFTP 站点管理、双栏浏览器、上传下载、
  断点续传、中文文件名编码热切换；**目录可整棵递归下载/上传**，并能
  **一键把本机文件夹变成网页分享**给别人
- **服务端**：`scripts/lan_share.py` 局域网 / Tailscale **Web 分享**——
  别人用浏览器就能下载你分享的文件/文件夹，无需安装任何软件
- Web 分享支持整文件夹 **ZIP 打包下载** 与 **上传整个文件夹**(递归建目录)，
  也支持反向上传文件(多文件、同名自动加序号、可选大小上限；`--no-upload` 可关闭)

## 运行环境

- Windows 10/11
- **Python 3.12+**（3.12 才修复 ftplib 在 FTPS 数据通道上的已知问题；本仓库在 3.14 上验证）

## 一、客户端：连接 FTPS/SFTP 服务器

```powershell
py -3.14 -m venv .venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe main.py
```

站点管理里添加服务器(FTPS 或 SFTP)，高级选项可调文件名编码、证书校验、被动模式等。

- 选中右侧**目录**(可多选)再点"下载所选"，会把整个目录树(含子目录、空目录)
  递归下载到本地；本地目录则可用"上传文件夹…"整棵上传到远端当前目录
- 工具栏"分享到网页…"：选一个本地文件夹即在本机起网页分享服务并列出
  局域网 / Tailscale 链接，发给对方即可浏览下载(文件夹可 ZIP 整包下载)，
  也能反向传文件给你

## 二、服务端：让"别人"访问你的电脑

### 场景 A：对方和你同一局域网，或都在你的 Tailscale 组网里

```powershell
# 1. 把要分享的文件放进 ./shared 文件夹(不存在会自动创建)
# 2. 启动分享服务(建议设密码)
.\venv\Scripts\python.exe scripts\lan_share.py --port 8000 --password 你的密码
# 3. 把打印出来的 http://你的IP:8000 发给对方
```

- 对方**用浏览器**打开网址、输入密码即可浏览下载：点文件直接下(支持断点续传/Range)，
  目录点 **ZIP** 按钮可把整棵目录树打包下载
- 想收文件? 页面上传框可传**文件**(多选、防覆盖改名)；另有"选择文件夹"框可把
  **整个文件夹**递归传上来(相对路径逐段清洗、自动建目录; 需新版 Chrome/Edge/Firefox)
- `--dir <路径>` 可分享任意文件夹；在桌面客户端点"分享到网页…"同样能一键起服务
- 脚本自动打印局域网地址；检测到 Tailscale 还会打印 100.x 组网地址
- 中文文件名、子目录浏览、目录穿越防护均已处理并有测试覆盖

### 场景 B：对方在外面，想访问你家这台电脑 → 用 Tailscale(你已安装)

Tailscale 免费组网：不需要公网 IP、不开放 22/21 端口、全程加密。

1. **本机**：登录 Tailscale(托盘图标可查本机 100.x.x.x 地址)
2. **对方**：安装并登录 Tailscale；你在 https://login.tailscale.com/admin 里把
   对方加为成员，或把本机 Share 给对方
3. 双方在线后，先运行 lan_share.py，再让对方浏览器访问：
   `http://<你的100.x.x.x>:8000`

> Windows 防火墙：Tailscale 网卡常被归为"公用网络"，对方打不开时，
> 请用**管理员** PowerShell 执行一次：
> `netsh advfirewall firewall add rule name="LANShare" dir=in action=allow protocol=TCP localport=8000`

### 免安装更省事：Tailscale Funnel(把链接公开给任何人)

在 Tailscale 管理台把 8000 端口设为 Funnel 后，对方**不需要装 Tailscale**，
直接浏览器访问 `https://机器名.组网名.ts.net` 即可。Funnel 属于"开给全网"，
务必只分享必要文件并设置强密码。

## 安全建议(重要)

1. **别把裸 FTP(21端口)直接暴露公网**——账号密码与数据都是明文。
2. 首选组网：Tailscale / WireGuard，无需端口映射。
3. 真用 FTP 就选 **FTPS(本项目默认)**；有 OpenSSH 服务器(Windows 可选功能)就选 SFTP。
4. 家用宽带通常没有公网 IPv4，路由器端口映射还常被运营商封 21。
5. 共享目录永远只放"该分享的文件"，别把整个盘/桌面/文档共享出去。

## 测试

```powershell
.\venv\Scripts\python.exe scripts\selftest.py    # 纯逻辑自测(路径/解析/存储/目录树递归)
.\venv\Scripts\python.exe scripts\e2e_ftps.py    # FTPS 端到端(先跑 dev_server.py; 含目录递归往返)
.\venv\Scripts\python.exe scripts\e2e_web.py     # Web 分享端到端(下载/上传/ZIP 打包/整文件夹上传)
.\venv\Scripts\python.exe scripts\gui_smoke.py   # GUI 离屏冒烟(可选 --with-server)
```

## 目录结构

```
main.py                 # 桌面客户端入口
core/                   # 纯逻辑层(标准库)：会话、存储、路径/列表解析、目录树递归传输(tree.py)
ui/                     # PySide6 界面：主窗口、站点管理、线程池 worker、网页分享对话框(share_dialog.py)
scripts/
  lan_share.py          # ★ 让别人用浏览器访问你的电脑(局域网/Tailscale, 整文件夹 ZIP/上传)
  dev_server.py         # 本地 FTPS 测试服务器(pyftpdlib, TLS)
  selftest.py / e2e_ftps.py / e2e_web.py / gui_smoke.py   # 自测/端到端/冒烟
```

## 站点配置存哪

```
%APPDATA%\FTPSharing\sites.json   # 站点(密码明文!)
%APPDATA%\FTPSharing\known_hosts  # SFTP 信任过的主机密钥
```

> 密码明文问题后续用 keyring 解决；测试隔离可用环境变量 FTPSHARING_CONFIG_DIR。

## 常见问题

- **客户端能连接但列不出目录/下载不动**：被动模式端口未放行；服务器在 NAT 后需配置
  被动端口范围并做映射。客户端默认 PASV。
- **中文文件名乱码**：站点编辑里改"文件名编码"为 GBK/GB18030，或连接后点"编码▾"热切换。
- **本机测试 FTPS 提示证书不受信任**：自签名证书正常，勾选"不校验服务器证书"。
- **Web 分享别人打不开**：① 确认同一网络/同一 tailnet；② 运行上面的防火墙放行命令；
  ③ 让对方用你的局域网/Tailscale IP，不是 127.0.0.1。

## 路线图 (Roadmap)

- [x] 客户端：站点管理 + FTPS/SFTP + 浏览 + 上传下载/续传/编码切换
- [x] 服务端：lan_share.py Web 分享(局域网 + Tailscale 地址识别 + Funnel 提示)
- [x] Web 分享支持上传(多文件/中文名/防覆盖/大小限制/只读开关)
- [x] Web 分享: 整文件夹 ZIP 打包下载 + 上传整个文件夹(递归建目录/清洗防护)
- [x] 客户端: 目录(文件夹)递归下载/上传(含空目录) + "一键网页分享本机文件夹"
- [x] 本地 FTPS 测试服务器 + 全套自测(selftest/e2e_ftps/e2e_web/gui_smoke)
- [ ] 传输队列、多任务并行
- [ ] keyring 密码存储
- [ ] PyInstaller 打包(onedir)

## License

见 LICENSE。