# 快速开始指南

## 环境配置（2分钟）

### 1. 复制环境变量模板

```bash
cp .env.example .env
```

### 2. 编辑.env文件

```bash
# 编辑数据库配置
nano .env  # 或使用其他编辑器
```

修改以下内容为您的实际配置：

```env
DB_HOST=your_database_host
DB_USER=your_database_user
DB_PASSWORD=your_database_password
DB_NAME=your_database_name
```

### 3. 验证配置

```bash
# 测试数据库连接
python -c "from database.db import engine; print('✓ 数据库连接成功' if engine.connect() else '✗ 连接失败')"
```

## 运行系统

### 方式1: 使用调度器（推荐）

```bash
python run_scheduler.py
```

### 方式2: 独立实时监控

```bash
python realtime_monitor.py
```

### 方式3: 单次运行

```bash
python main.py
```

## 常见问题

### Q: 数据库连接失败？

A: 检查.env文件中的数据库配置是否正确，确保MySQL服务已启动。

### Q: Futu API连接失败？

A: 确保FutuOpenD已安装并运行，默认端口为11111。

### Q: 如何查看系统状态？

A: 系统会实时输出日志信息，包括：
- 实时报价更新
- 性能统计
- 交易信号

## 下一步

- 📖 阅读完整文档：[SETUP.md](SETUP.md)
- 🚀 性能优化：[BATCH_WRITING_OPTIMIZATION.md](BATCH_WRITING_OPTIMIZATION.md)
- 📖 项目说明：[README.md](README.md)

## 安全提醒

⚠️ **重要**：.env文件包含敏感信息，已被.gitignore忽略，不会提交到Git。

🎯