# RigolDptAnalyzer

RigolDptAnalyzer 是一个用于解析 Rigol `RG01` 示波器 `.bin` 文件，并自动计算同步 Buck 双脉冲测试 `Eon/Eoff` 的可复用工具包。

## 1. 适用场景

默认测试拓扑：

```text
BUS+ -> Q1 上管 -> SW / Drain2 -> CT1 / 电感接口 -> 电感 -> 短接负载 -> BUS-
                         |
                       Q2 下管
                         |
                       BUS-
```

默认损耗口径：

```text
E_sw_Q1 = Eoff_Q1(first) + Eon_Q1(second)
```

也就是第一段 Q1 关断损耗加第二段 Q1 开通损耗。Q2 作为同步续流器件，其恢复、Coss、死区和第三象限导通影响主要体现在第二次 Q1 开通损耗中。

## 2. 文件结构

```text
RigolDptAnalyzer
├─ src
│  └─ rigol_dpt_analyzer.py        # 主程序
├─ docs
│  └─ rigol_bin_eon_eoff_program.md # 算法和参数说明
├─ examples
│  ├─ analyze_current_dataset.ps1   # 当前数据集示例
│  ├─ analyze_folder.ps1            # 通用目录分析示例
│  └─ analyze_ct_voltage.ps1        # CT 电压换算示例
├─ sample_output
│  ├─ summary.csv                   # 小体积示例汇总
│  └─ analysis_report.md            # 小体积示例报告
├─ install_dependencies.ps1         # 安装依赖
├─ run_analysis.ps1                 # Windows PowerShell 入口
├─ run_analysis.bat                 # Windows CMD 入口
├─ requirements.txt
└─ VERSION.txt
```

## 3. 安装依赖

在 PowerShell 中执行：

```powershell
cd "D:\path\to\RigolDptAnalyzer"
.\install_dependencies.ps1
```

依赖：

```text
numpy
matplotlib
```

其中 `matplotlib` 只用于生成 PNG 图；如果只需要 CSV/JSON/Markdown，可不加 `--Plot`。

## 4. 快速运行

分析某个文件夹中的全部 `.bin`：

```powershell
.\run_analysis.ps1 `
  -InputPath "D:\data\rigol_bin" `
  -Output "D:\data\rigol_bin\analysis_out" `
  -SaveWaveforms `
  -Plot
```

分析单个 `.bin`：

```powershell
.\run_analysis.ps1 `
  -InputPath "D:\data\RigolDS5.bin" `
  -Output "D:\data\analysis_out" `
  -Plot
```

从 CMD 调用：

```bat
run_analysis.bat -InputPath "D:\data" -Output "D:\data\analysis_out" -Plot
```

## 5. 关键参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-InputPath` | 必填 | `.bin` 文件或包含 `.bin` 的目录，可传多个 |
| `-Output` | `.\dpt_analysis_out` | 输出目录 |
| `-VdsTrace` | `2` | Vds 类信号所在 trace 序号 |
| `-CurrentTrace` | `3` | 电流信号所在 trace 序号 |
| `-VdsInput` | `q1` | `q1` 表示输入已是 Q1 Vds；`q2` 表示输入是 Q2 Vds，需要转换 |
| `-CurrentMode` | `direct` | `direct` 表示电流已是 A；`ct-voltage` 表示 CT 电压需换算 |
| `-VbusMode` | `global` | `global` 使用全局平台电压；`local` 使用边沿附近平台电压 |
| `-SaveWaveforms` | 关闭 | 保存完整波形 CSV |
| `-Plot` | 关闭 | 保存 PNG 波形图 |

如果 CT1 输出是原始电压，使用：

```powershell
.\run_analysis.ps1 `
  -InputPath "D:\data" `
  -Output "D:\data\analysis_out" `
  -CurrentMode ct-voltage `
  -CtZeroV 1.65 `
  -CtVPerA 0.00625
```

## 6. 输出文件

| 文件 | 内容 |
|---|---|
| `summary.csv` | 每个 bin 的 T1/T2/T3、Ioff/Ion、Eoff/Eon/E_sw 汇总 |
| `analysis_refined.json` | 完整结构化结果 |
| `analysis_report.md` | 自动生成 Markdown 报告 |
| `RigolDS*.csv` | 可选，完整波形 CSV |
| `RigolDS*.png` | 可选，全窗口波形图 |
| `RigolDS*_zoom.png` | 可选，双脉冲窗口放大图 |

## 7. 注意事项

- 如果 CH2 接的是 Q2 Vds，要设置 `-VdsInput q2`，否则 Q1 损耗归属会错。
- 如果电流不是直接以 A 保存，要设置 `-CurrentMode ct-voltage` 并确认 CT 零点和比例。
- 当前损耗是工程估算，因为通常没有直接采集 Q1 高带宽漏极电流。
- 正式损耗报告前应做 Vds 和电流探头 deskew；SiC 边沿十几 ns 的延时足以造成明显能量误差。
- 默认 `E_linear` 作为推荐工程估算，`E_upper` 作为上限参考。
