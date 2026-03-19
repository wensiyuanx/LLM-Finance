# 环境配置说明

## 快速开始

### 1. 复制环境变量模板

```bash
cp .env.example .env
```

### 2. 编辑.env文件

根据您的实际环境修改以下配置：

```bash
# Futu API setup
FUTU_HOST=127.0.0.1  # FutuOpenD服务器地址
FUTU_PORT=11111      # FutuOpenD端口

# MySQL Database setup
DB_HOST=your_database_host      # 数据库地址
DB_PORT=3306                    # 数据库端口
DB_USER=your_database_user      # 数据库用户名
DB_PASSWORD=your_database_password  # 数据库密码
DB_NAME=your_database_name      # 数据库名称
```

## 配置说明

### Futu API配置

- **FUTU_HOST**: FutuOpenD服务器地址，默认本地安装为 `127.0.0.1`
- **FUTU_PORT**: FutuOpenD端口，默认为 `11111`

**获取FutuOpenD**:
1. 下载FutuOpenD: https://openapi.futunn.com/
2. 安装并启动FutuOpenD
3. 确保端口配置正确

### MySQL数据库配置

- **DB_HOST**: 数据库服务器地址
- **DB_PORT**: 数据库端口，默认MySQL为 `3306`
- **DB_USER**: 数据库用户名
- **DB_PASSWORD**: 数据库密码
- **DB_NAME**: 数据库名称

**数据库初始化**:

系统会自动创建所需的数据库表，但需要先确保：

1. MySQL服务器已安装并运行
2. 已创建数据库（或配置有创建数据库权限的用户）
3. 用户权限足够（CREATE, ALTER, INSERT, UPDATE, DELETE, SELECT）

## 安全注意事项

### ⚠️ 重要提醒

1. **不要提交.env文件到Git**
   - `.env` 文件包含敏感信息（数据库密码等）
   - 已在 `.gitignore` 中配置忽略

2. **使用强密码**
   - 数据库密码应该足够复杂
   - 定期更换密码

3. **限制数据库访问**
   - 使用专用数据库用户
   - 限制IP访问
   - 使用最小权限原则

### 生产环境建议

- 使用环境变量管理服务（如AWS Secrets Manager、Azure Key Vault）
- 定期轮换密钥
- 启用数据库连接加密
- 配置数据库防火墙

## 故障排查

### 连接FutuOpenD失败

```
Error: Failed to connect to FutuOpenD
```

**解决方案**:
1. 确认FutuOpenD已启动
2. 检查FUTU_HOST和FUTU_PORT配置
3. 确认网络连接正常
4. 检查防火墙设置

### 数据库连接失败

```
Error: Can't connect to MySQL server
```

**解决方案**:
1. 确认MySQL服务器已启动
2. 检查数据库地址和端口
3. 验证用户名和密码
4. 确认数据库已创建
5. 检查用户权限

### 权限问题

```
Error: Access denied for user
```

**解决方案**:
1. 确认数据库用户名和密码正确
2. 检查用户权限
3. 确认数据库存在

## 配置验证

### 测试数据库连接

```python
from database.db import engine, SessionLocal

# 测试连接
try:
    with engine.connect() as conn:
        print("✓ 数据库连接成功")
except Exception as e:
    print(f"✗ 数据库连接失败: {e}")
```

### 测试Futu API连接

```python
from data.futu_client import FutuClient

# 测试连接
futu = FutuClient()
if futu.connect():
    print("✓ Futu API连接成功")
    futu.close()
else:
    print("✗ Futu API连接失败")
```

## 开发环境配置

### 本地开发

```bash
# 1. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或 venv\Scripts\activate  # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑.env文件

# 4. 初始化数据库
python -c "from database.db import init_db; init_db()"

# 5. 运行系统
python run_scheduler.py
```

### Docker部署

```bash
# 使用docker-compose
docker-compose up -d
```

## 环境变量优先级

系统按以下优先级读取配置：

1. 系统环境变量
2. .env文件
3. 代码中的默认值

## 常见问题

### Q: 如何在生产环境中管理敏感信息？

A: 建议使用专业的密钥管理服务，如：
- AWS Secrets Manager
- Azure Key Vault
- HashiCorp Vault

### Q: 可以使用其他数据库吗？

A: 当前版本仅支持MySQL，可以通过修改SQLAlchemy连接字符串支持其他数据库。

### Q: 如何修改数据库端口？

A: 在.env文件中修改DB_PORT参数。

## 支持

如有问题，请查看：
- 项目README.md
- FutuOpenD官方文档
- MySQL官方文档

🎯