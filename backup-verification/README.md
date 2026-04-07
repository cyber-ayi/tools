# Backup Verification Tool

SD 卡原始数据与 NAS 备份之间的校验工具。支持文件名变更后的匹配（按文件大小 + SHA-256 校验），EXIF 元数据差异检测，多线程哈希，SQLite 缓存加速。

## 文件结构

```
verify.py              批量入口脚本，读取配置文件执行多个验证任务
verify_backup.py       核心验证逻辑
verify_config.json          任务配置文件（gitignore，含本地路径）
verify_config.json.example  配置模板
test_verify_backup.py  单元测试
pyproject.toml         Poetry 项目配置
.python-version        pyenv Python 版本锁定
```

## 环境搭建

### 安装 pyenv (Windows)

```powershell
# 方式 1: 使用 pyenv-win 安装脚本
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1" -OutFile "./install-pyenv-win.ps1"; &"./install-pyenv-win.ps1"

# 方式 2: 使用 scoop
scoop install pyenv

# 方式 3: 使用 choco
choco install pyenv-win
```

重启终端后安装 Python：

```powershell
pyenv install 3.12.8
pyenv local 3.12.8      # 项目级别锁定（已有 .python-version）
python --version         # 确认 Python 3.12.8
```

### 安装 Poetry

```powershell
# 官方安装脚本
(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | python -

# 或使用 pipx
pipx install poetry
```

### 初始化项目

```powershell
cd M:\workspace\claude

# 从模板创建配置文件
copy verify_config.json.example verify_config.json
# 编辑 verify_config.json，填入实际的 src/dest 路径

poetry install           # 创建虚拟环境 + 安装依赖
poetry run pytest -v     # 运行测试
```

### 日常使用

```powershell
# 通过 poetry 运行（推荐）
poetry run python verify_backup.py "O:\DCIM\100_FUJI" "W:\storage\ingest\FUJIFILM X-T5"
poetry run python verify.py

# 或先激活虚拟环境
poetry shell
python verify_backup.py "O:\DCIM\100_FUJI" "W:\storage\ingest\FUJIFILM X-T5"
```

## 快速开始

### 单次验证

```bash
python verify_backup.py "O:\DCIM\100_FUJI" "W:\storage\ingest\FUJIFILM X-T5"
```

### 批量验证

1. 编辑 `verify_config.json` 配置任务
2. 运行 `python verify.py`

## verify_backup.py

核心验证脚本，比较源目录与目标目录中的文件。

### 匹配策略

由于备份工具可能重命名文件（如 `DSCF0050.JPG` → `2026-03-28_18-24-59_DSCF0050.JPG`），匹配不依赖文件名，而是：

1. 按**文件大小**分组，找到候选文件
2. 按 **SHA-256** 校验确认一致性

### 比较模式 (`--mode`)

| 模式 | 说明 |
|---|---|
| `full` | 仅全文件 SHA-256 比较。最严格，EXIF 差异也会报 MISMATCH |
| `smart` | **默认**。全文件 hash 优先；JPEG mismatch 时自动回退到图像数据 hash，区分 METADIFF 和 MISMATCH |
| `data-only` | JPEG 仅比较图像数据 hash（从 SOS marker 到 EOF），完全忽略 EXIF 元数据 |

### 结果分类

| 状态 | 含义 |
|---|---|
| **OK** | 文件完全匹配（或 data-only 模式下图像数据匹配） |
| **METADIFF** | 全文件 hash 不同但图像数据一致，仅 EXIF 元数据被修改（仅 smart 模式） |
| **MISSING** | 源文件在目标目录中未找到（无同大小文件） |
| **MISMATCH** | 文件大小相同但校验不通过，可能数据损坏 |

### 退出码

- `0` — 所有文件通过验证（METADIFF 视为通过）
- `1` — 存在 MISSING 或 MISMATCH

使用 `--strict` 可将 METADIFF 也视为失败。

### 命令行参数

```
python verify_backup.py <src_dir> <dest_dir> [options]

位置参数:
  src_dir                源目录（SD 卡）
  dest_dir               目标目录（NAS 备份）

可选参数:
  -w, --workers N        哈希线程数（默认 4）
  -m, --mode MODE        比较模式: full, smart, data-only（默认 smart）
  -o, --output FILE      报告文件路径（默认自动生成）
  --strict               METADIFF 视为失败
  --no-cache             不使用哈希缓存
  --clear-cache          清除现有缓存并重建
```

### 示例

```bash
# smart 模式，8 线程
python verify_backup.py "O:\DCIM\100_FUJI" "W:\storage\ingest\FUJIFILM X-T5" -w 8

# 严格模式
python verify_backup.py "O:\DCIM\100_FUJI" "W:\storage\ingest\FUJIFILM X-T5" --strict

# 仅比较图像数据
python verify_backup.py "O:\DCIM\100_FUJI" "W:\storage\ingest\FUJIFILM X-T5" --mode data-only

# 不使用缓存
python verify_backup.py "O:\DCIM\100_FUJI" "W:\storage\ingest\FUJIFILM X-T5" --no-cache
```

## verify.py

批量入口脚本，从 `verify_config.json` 读取任务列表并依次执行。

### 命令行参数

```
python verify.py [options]

  -c, --config FILE      配置文件路径（默认 verify_config.json）
  -l, --list             列出所有任务
  --only NAME [NAME...]  仅运行指定名称的任务
  -w, --workers N        覆盖线程数
  -m, --mode MODE        覆盖比较模式
  --strict               METADIFF 视为失败
  --no-cache             禁用缓存
  --clear-cache          清除并重建缓存
```

### 示例

```bash
python verify.py                              # 运行所有 enabled 的任务
python verify.py --list                       # 查看配置的任务
python verify.py --only "FUJIFILM X-T5"       # 只运行指定任务
python verify.py -w 16 --mode smart           # 覆盖全局设置
```

## verify_config.json

```json
{
  "workers": 16,
  "mode": "smart",
  "output_dir": "./reports",
  "jobs": [
    {
      "name": "FUJIFILM X-T5",
      "src": "O:\\DCIM\\100_FUJI",
      "dest": "W:\\storage\\ingest\\FUJIFILM X-T5",
      "enabled": true,
      "workers": 8,
      "mode": "smart"
    }
  ]
}
```

### 字段说明

| 字段 | 层级 | 说明 |
|---|---|---|
| `workers` | 全局/任务 | 哈希线程数 |
| `mode` | 全局/任务 | 比较模式（CLI > 任务 > 全局 > smart） |
| `output_dir` | 全局 | 报告保存目录 |
| `jobs[].name` | 任务 | 任务名称（用于 `--only` 筛选和报告文件名） |
| `jobs[].src` | 任务 | 源目录路径 |
| `jobs[].dest` | 任务 | 目标目录路径 |
| `jobs[].enabled` | 任务 | 是否默认启用（默认 true） |

## 哈希缓存

目标目录下自动创建 `.verify_cache.db`（SQLite），缓存已计算的文件哈希。

### 缓存验证

每次运行时自动校验缓存有效性：

- **Valid** — 文件 size + mtime 未变，复用缓存的 hash
- **Stale** — 文件存在但 size 或 mtime 变了，重新计算
- **New** — 文件不在缓存中，新计算
- **Removed** — 缓存中的文件已删除，自动清理

### 数据库结构

```sql
CREATE TABLE hash_cache (
    path        TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    data_sha256 TEXT              -- JPEG 图像数据哈希（可为 NULL）
);
```

### 缓存管理

```bash
--no-cache       跳过缓存，所有文件重新计算
--clear-cache    删除缓存文件后重建
```

## 单次读取双哈希 (sha256_dual)

smart/data-only 模式对 JPEG 文件使用 `sha256_dual()`，单次文件读取同时计算全文件 hash 和图像数据 hash：

1. `find_jpeg_sos()` 解析 JPEG marker 结构定位 SOS marker (`0xFFDA`)
2. 顺序读取文件，每个 chunk 同时 update 两个 SHA-256 对象
3. SOS 之前的数据仅计入全文件 hash，SOS 之后的数据同时计入两个 hash

相比分别调用两次哈希函数，I/O 减半。

## 测试

```bash
# 推荐（通过 poetry）
poetry run pytest -v

# 或直接运行
python -m pytest test_verify_backup.py -v
python -m unittest test_verify_backup -v
```

### 测试覆盖

- JPEG 解析：SOS marker 定位、截断文件、非 JPEG 文件
- 双哈希：EXIF 差异检测、图像数据差异检测、跨 chunk 边界
- 缓存：创建/读取/验证/迁移/同步
- 端到端：所有比较模式 × 所有结果类型、严格模式、缓存持久化

## 要求

- Python 3.8+（推荐 3.12，使用 walrus operator / f-strings / type hints）
- 无第三方运行时依赖（仅使用标准库）
- 开发依赖：pytest（通过 `poetry install` 安装）
