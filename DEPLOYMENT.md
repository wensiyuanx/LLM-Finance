# 部署说明

## 环境变量问题修复

### 问题描述
在服务器环境部署时，富途API需要 `HOME` 环境变量，但某些服务器环境可能没有设置这个变量，导致以下错误：

```
KeyError: 'HOME'
```

### 解决方案
已在以下文件中添加环境变量检查和修复：
- `run_scheduler.py`
- `main.py`
- `realtime_monitor.py`

### 修复代码
```python
import os

# Fix for FuTu API requiring HOME environment variable
if 'HOME' not in os.environ:
    os.environ['HOME'] = os.getcwd()
```

## 部署步骤

### 1. 上传文件到服务器
将以下文件上传到服务器目录 `/www/wwwroot/llm-transaction-futu/`：
- `run_scheduler.py` (修复后)
- `main.py` (修复后)
- `realtime_monitor.py` (修复后)
- 其他项目文件

### 2. 安装依赖
```bash
cd /www/wwwroot/llm-transaction-futu
pip install -r requirements.txt
```

### 3. 配置环境变量
确保 `.env` 文件配置正确：
```env
FUTU_HOST=127.0.0.1
FUTU_PORT=11111
DB_HOST=your_database_host
DB_PORT=3306
DB_USER=your_database_user
DB_PASSWORD=your_database_password
DB_NAME=your_database_name
```

### 4. 初始化数据库
```bash
python scripts/init_db.py
```

### 5. 启动富途OpenD
确保富途OpenD在服务器上运行并监听 `127.0.0.1:11111`

### 6. 测试运行
```bash
# 测试单次运行
python main.py

# 测试调度器（运行几秒后 Ctrl+C 停止）
python run_scheduler.py
```

### 7. 生产部署
使用 nohup 或 systemd 在后台运行：

#### 方法1：使用 nohup
```bash
nohup python run_scheduler.py > bot_output.log 2>&1 &
```

#### 方法2：使用 systemd（推荐）
创建 `/etc/systemd/system/quant-bot.service`：
```ini
[Unit]
Description=Quant Trading Bot
After=network.target

[Service]
Type=simple
User=www
WorkingDirectory=/www/wwwroot/llm-transaction-futu
ExecStart=/usr/bin/python3 run_scheduler.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable quant-bot
sudo systemctl start quant-bot
sudo systemctl status quant-bot
```

## 监控和日志

### 查看日志
```bash
# 实时查看日志
tail -f bot.log

# 查看最近100行
tail -n 100 bot.log

# 查看错误日志
grep ERROR bot.log
```

### 检查进程
```bash
# 查看进程是否运行
ps aux | grep run_scheduler

# 查看端口占用
netstat -tuln | grep 11111
```

## 常见问题

### 1. 富途API连接失败
**错误**: `Connect fail: ECONNREFUSED`

**解决**:
- 确保富途OpenD正在运行
- 检查端口 11111 是否被占用
- 检查防火墙设置

### 2. 数据库连接失败
**错误**: `Can't connect to MySQL server`

**解决**:
- 检查 `.env` 文件中的数据库配置
- 确保数据库服务正在运行
- 检查数据库用户权限

### 3. 权限问题
**错误**: `Permission denied`

**解决**:
```bash
# 修改文件权限
chmod +x run_scheduler.py
chown -R www:www /www/wwwroot/llm-transaction-futu
```

### 4. 内存不足
**错误**: `MemoryError`

**解决**:
- 增加服务器内存
- 优化数据获取的时间窗口
- 清理旧的日志文件

## 更新部署

### 更新代码
```bash
cd /www/wwwroot/llm-transaction-futu
git pull  # 如果使用git
# 或手动上传新文件

# 重启服务
sudo systemctl restart quant-bot
```

### 备份数据
```bash
# 备份数据库
mysqldump -u username -p database_name > backup.sql

# 备份配置文件
cp .env .env.backup
```

## 安全建议

1. **保护敏感信息**
   - `.env` 文件不要提交到git
   - 使用强密码
   - 定期更换密码

2. **网络安全**
   - 使用防火墙限制访问
   - 数据库只允许本地连接
   - 定期更新系统和依赖

3. **日志管理**
   - 定期清理旧日志
   - 避免在日志中记录敏感信息
   - 设置日志轮转

## 性能优化

### 1. 减少日志级别
在生产环境中，可以将日志级别改为 WARNING：
```python
logging.basicConfig(level=logging.WARNING)
```

### 2. 优化数据库查询
- 添加索引
- 使用连接池
- 定期清理旧数据

### 3. 资源监控
```bash
# 查看内存使用
free -h

# 查看磁盘使用
df -h

# 查看CPU使用
top
```

## 联系支持

如遇到问题，请检查：
1. `bot.log` 日志文件
2. 系统日志 `/var/log/syslog`
3. 富途OpenD日志
