# Backup Verification Tool

SD 卡原始数据与 NAS 备份之间的校验工具。支持文件名变更后的匹配（按文件大小 + SHA-256 校验），EXIF 元数据差异检测，多线程哈希，SQLite 缓存加速。

## 文件结构

```
verify.py              统一入口（单次验证 + 批量验证）
verify_backup.py       核心验证逻辑（可作为模块 import）
verify_config.json          任务配置文件（gitignore，含本地路径）
verify_config.json.example  配置模板
test_verify_backup.py  单元测试（59 项）
pyproject.toml         项目配置
.python-version        pyenv Python 版本锁定
```

## 环境搭建

### 安装 Python

推荐使用 pyenv 管理 Python 版本：

```powershell
# 安装 pyenv-win
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1" -OutFile "./install-pyenv-win.ps1"; &"./install-pyenv-win.ps1"

# 安装并锁定 Python 版本
pyenv install 3.12.10
pyenv local 3.12.10
```

### 创建虚拟环境

```powershell
cd M:\workspace\tools\backup-verification

python -m venv .venv
.venv\Scripts\activate
pip install pytest

# 运行测试
python -m pytest test_verify_backup.py -v
```

### 初始化配置

```powershell
# 从模板创建配置文件
copy verify_config.json.example verify_config.json
# 编辑 verify_config.json，填入实际的 src/dest 路径（使用正斜杠）
```

## 快速开始

### 单次验证

```bash
python verify.py "O:/DCIM/100_FUJI" "W:/storage/ingest/FUJIFILM X-T5"

# 指定模式和线程数
python verify.py "O:/DCIM/100_FUJI" "W:/storage/ingest/FUJIFILM X-T5" -w 8 --mode smart

# 详细输出（显示每个文件的匹配结果）
python verify.py "O:/DCIM/100_FUJI" "W:/storage/ingest/FUJIFILM X-T5" -v
```

### 批量验证

```bash
python verify.py -c verify_config.json             # 运行所有 enabled 的任务
python verify.py -c verify_config.json --list       # 查看配置的任务
python verify.py -c verify_config.json --only "FUJIFILM X-T5"  # 只运行指定任务
python verify.py                                    # 自动使用默认配置文件
```

### 预览模式 (dry-run)

扫描目录并显示缓存命中率，不执行哈希计算：

```bash
python verify.py "O:/DCIM/100_FUJI" "W:/storage/ingest/FUJIFILM X-T5" --dry-run

# 输出示例：
# Dry-run results:
#   Source files : 931 (31.5 GB)
#   Dest files   : 1204 (42.1 GB)
#   Candidates   : 931 (size-matched dest files)
#   Cache hits   : 820 / 931 (88.1%)
#   Need hashing : 111 files (3.8 GB)
```

## 统一入口 (verify.py)

`verify.py` 是唯一推荐的入口，支持两种模式：

- **单次模式**：`verify.py <src> <dest> [options]` — 直接传源目录和目标目录
- **批量模式**：`verify.py -c config.json [options]` — 从配置文件读取任务列表

### 命令行参数

```
python verify.py <src_dir> <dest_dir> [options]
python verify.py -c CONFIG [options]

位置参数（单次模式）:
  src_dir                源目录（SD 卡）
  dest_dir               目标目录（NAS 备份）

批量模式:
  -c, --config FILE      配置文件路径
  -l, --list             列出所有任务
  --only NAME [NAME...]  仅运行指定名称的任务

通用参数:
  -w, --workers N        哈希线程数（默认自动检测，上限 16）
  -m, --mode MODE        比较模式: full, smart, data-only（默认 smart）
  -o, --output FILE      报告文件路径（单次模式，默认自动生成）
  -v, --verbose          详细输出（显示每个文件的结果，报告含 MATCHED FILES）
  -n, --dry-run          仅扫描，显示缓存命中率，不执行哈希
  --strict               METADIFF 视为失败
  --no-cache             不使用哈希缓存
  --clear-cache          清除现有缓存并重建
```

## 匹配策略

由于备份工具可能重命名文件（如 `DSCF0050.JPG` → `2026-03-28_18-24-59_DSCF0050.JPG`），匹配不依赖文件名，而是：

1. 按**文件大小**分组，找到候选文件
2. 按 **SHA-256** 校验确认一致性

## 比较模式 (`--mode`)

| 模式 | 说明 |
|---|---|
| `full` | 仅全文件 SHA-256 比较。最严格，EXIF 差异也会报 MISMATCH |
| `smart` | **默认**。全文件 hash 优先；JPEG mismatch 时自动回退到图像数据 hash，区分 METADIFF 和 MISMATCH |
| `data-only` | JPEG 仅比较图像数据 hash（从 SOS marker 到 EOF），完全忽略 EXIF 元数据 |

## 结果分类

| 状态 | 含义 |
|---|---|
| **OK** | 文件完全匹配（或 data-only 模式下图像数据匹配） |
| **METADIFF** | 全文件 hash 不同但图像数据一致，仅 EXIF 元数据被修改（仅 smart 模式） |
| **MISSING** | 源文件在目标目录中未找到（无同大小文件） |
| **MISMATCH** | 文件大小相同但校验不通过，可能数据损坏 |
| **ERROR** | 文件无法读取（权限错误/IO 错误），跳过 |

### 退出码

- `0` — 所有文件通过验证（METADIFF 视为通过）
- `1` — 存在 MISSING、MISMATCH 或 ERROR

使用 `--strict` 可将 METADIFF 也视为失败。

## 输出模式

### 默认（quiet）

终端仅显示进度条和异常结果：

```
  [450/931  48%] Verifying...
  MISSING  lost_file.jpg
  MISMATCH corrupted.jpg (checksum differs)
```

完整结果始终写入报告文件。

### 详细 (`-v`)

显示每个文件的匹配结果，报告中包含 MATCHED FILES 列表：

```
  [1/931   0%] OK       DSCF0001.JPG -> 2026-03-28_DSCF0001.JPG
  [2/931   0%] METADIFF DSCF0002.JPG -> 2026-03-28_DSCF0002.JPG (EXIF differs, image data OK)
```

## verify_config.json

配置文件中的路径**统一使用正斜杠**，工具会自动适配当前操作系统：

```json
{
  "workers": 16,
  "mode": "smart",
  "output_dir": "./reports",
  "jobs": [
    {
      "name": "FUJIFILM X-T5",
      "src": "O:/DCIM/100_FUJI",
      "dest": "W:/storage/ingest/FUJIFILM X-T5",
      "enabled": true,
      "workers": 8,
      "mode": "smart"
    }
  ]
}
```

> **注意**：JSON 中反斜杠需要转义（`\\`），容易出错。推荐始终使用正斜杠 `/`。

### 字段说明

| 字段 | 层级 | 说明 |
|---|---|---|
| `workers` | 全局/任务 | 哈希线程数（默认自动检测） |
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
--dry-run        查看缓存命中率（不执行哈希）
```

## 单次读取双哈希 (sha256_dual)

smart/data-only 模式对 JPEG 文件使用 `sha256_dual()`，单次文件读取同时计算全文件 hash 和图像数据 hash：

1. `find_jpeg_sos()` 解析 JPEG marker 结构定位 SOS marker (`0xFFDA`)
2. 顺序读取文件，每个 chunk 同时 update 两个 SHA-256 对象
3. SOS 之前的数据仅计入全文件 hash，SOS 之后的数据同时计入两个 hash

相比分别调用两次哈希函数，I/O 减半。

## 错误容忍

工具在遇到单文件错误（权限拒绝、网络断开等）时不会中断整体流程：

- `scan_dir()` 跳过无法 stat 的文件，记录错误
- `hash_dest()` / `verify_one()` 捕获 `OSError`，标记为 ERROR
- 所有错误详情写入报告的 ERROR / SCAN ERRORS section

## 测试

```bash
# 通过 venv
.venv/Scripts/activate
python -m pytest test_verify_backup.py -v

# 直接运行
python -m pytest test_verify_backup.py -v
python -m unittest test_verify_backup -v
```

### 测试覆盖（59 项）

- JPEG 解析：SOS marker 定位、截断文件、非 JPEG 文件
- 双哈希：EXIF 差异检测、图像数据差异检测、跨 chunk 边界
- 缓存：创建/读取/验证/迁移/同步
- 端到端：所有比较模式 × 所有结果类型、严格模式、缓存持久化
- UX：quiet/verbose 输出、空目录警告、dry-run、统一入口（单次 + 批量）、配置路径标准化、JSON 错误提示

## 要求

- Python 3.8+（推荐 3.12，使用 walrus operator / f-strings / type hints）
- 无第三方运行时依赖（仅使用标准库）
- 开发依赖：pytest
