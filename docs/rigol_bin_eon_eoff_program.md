# Rigol bin 双脉冲 Eon/Eoff 自动分析程序说明

## 1. 程序位置

程序文件：

```text
.\src\rigol_dpt_analyzer.py
```

默认分析对象为本项目当前的同步 Buck 半桥双脉冲数据：

```text
BUS+ -> Q1 上管 -> SW / Drain2 -> CT1 / 电感接口 -> 电感 -> 短接负载 -> BUS-
                         |
                       Q2 下管
                         |
                       BUS-
```

标准损耗口径为：

```text
E_sw_Q1 = Eoff_Q1(first) + Eon_Q1(second)
```

即第一段 Q1 关断损耗加第二段 Q1 开通损耗。Q2 是同步续流对象，Q2 的第三象限导通、死区、Coss 和恢复影响主要体现在第二次 Q1 开通损耗中。

## 2. 一键运行示例

分析当前文件夹下所有 `.bin`：

```powershell
.\run_analysis.ps1 `
  -InputPath "D:\data\rigol_bin" `
  -Output "D:\data\rigol_bin\analysis_out" `
  -SaveWaveforms `
  -Plot
```

只分析指定 6 个文件：

```powershell
.\run_analysis.ps1 `
  -InputPath "D:\data\RigolDS0.bin","D:\data\RigolDS1.bin","D:\data\RigolDS2.bin" `
  -Output "D:\data\analysis_out" `
  -SaveWaveforms `
  -Plot
```

## 3. 输入通道假设

默认通道映射：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `--vds-trace` | 2 | 第 2 条 trace 作为 Vds 类电压 |
| `--current-trace` | 3 | 第 3 条 trace 作为电流 |
| `--vds-input` | `q1` | Vds trace 已经是 Q1 Vds |
| `--current-mode` | `direct` | 电流 trace 已经是 A |

如果 CH2 实际接的是 Q2 Vds，而要计算 Q1 损耗，使用：

```powershell
--vds-input q2
```

程序会按近似关系转换：

```text
Vds_Q1 ~= Vbus - Vds_Q2
```

如果示波器保存的是 CT1 原始电压，不是已经换算好的电流，使用：

```powershell
--current-mode ct-voltage --ct-zero-v 1.65 --ct-v-per-a 0.00625
```

换算公式：

```text
I = (Vct - 1.65 V) / 0.00625 V/A
```

## 4. Rigol bin 解析流程

当前 Rigol 文件为 `RG01` 二进制格式。程序按以下规则解析：

1. 文件开头 4 字节为 `RG01`。
2. 第 8 字节处读取 trace 数量。
3. 每个 trace 块包含一个 152 字节左右的头部和后续 float32 数据。
4. trace 头部 `offset + 12` 处读取点数。
5. trace 头部 `offset + 20` 处读取总时间窗 `total_span_s`。
6. trace 数据按 little-endian float32 读取，示波器已保存为物理量单位。

时间轴构造：

```text
dt = total_span_s / points
t[i] = (i - points/2) * dt
```

注意：这里 `offset + 20` 字段按总时间窗处理，不按单格 time/div 处理；否则时间轴会放大 10 倍。

## 5. 脉冲和边沿识别

程序先从 Q1 Vds 中识别高低电平：

1. 对 Vds 采样点做两类聚类，得到低电平 `Vlow` 和高电平 `Vbus`。
2. 阈值取 `(Vlow + Vbus) / 2`。
3. 因为 Q1 导通时 Q1 Vds 为低电平，所以：

```text
Q1_on = Vds_Q1 < threshold
```

4. 对短毛刺做去抖合并，默认 `--debounce-ns 20`。
5. 保留长度大于 `--min-pulse-us 0.5` 的低电平区间。
6. 默认取前两个 Q1 导通区间作为 T1 和 T3，中间间隔作为 T2。

## 6. Eon/Eoff 积分窗口

每个 Q1 低电平脉冲对应两个边沿：

| 边沿 | 物理含义 | 损耗类型 |
|---|---|---|
| Vds 高到低 | Q1 开通 | `Eon` |
| Vds 低到高 | Q1 关断 | `Eoff` |

积分窗口按 Vds 的 10% 到 90% 过渡区间确定：

```text
Eon：Vds 从 90% Vbus 降到 10% Vbus
Eoff：Vds 从 10% Vbus 升到 90% Vbus
```

边沿电流 `I_edge` 默认取边沿前 `100 ns` 的电流中值：

```powershell
--current-window-ns 100
```

## 7. 损耗计算公式

程序输出两个能量值：

### 7.1 上限估算 E_upper

假设整个 Vds 过渡窗口内 DUT 电流等于边沿电流：

```text
E_upper = I_edge * sum(Vds * dt)
```

该值偏保守，主要作为上限参考。

### 7.2 线性换流估算 E_linear

假设开关过程中电流与电压线性互补换流：

```text
I_overlap = I_edge * (1 - Vds / Vbus)
E_linear = sum(Vds * I_overlap * dt)
```

报告中的推荐工程估算值使用 `E_linear`。

标准双脉冲主指标为：

```text
Eoff_Q1_first = 第一次 Q1 关断 E_linear
Eon_Q1_second = 第二次 Q1 开通 E_linear
E_sw_Q1 = Eoff_Q1_first + Eon_Q1_second
```

## 8. 输出文件

运行后输出目录包含：

| 文件 | 内容 |
|---|---|
| `summary.csv` | 每个 bin 的 T1/T2/T3、Ioff/Ion、Eoff/Eon/E_sw 汇总 |
| `analysis_refined.json` | 完整结构化结果，包含 header、统计值、脉冲、每个边沿损耗 |
| `analysis_report.md` | 自动生成的 Markdown 报告 |
| `RigolDS*.csv` | 可选，`--save-waveforms` 生成的完整波形 CSV |
| `RigolDS*.png` | 可选，`--plot` 生成的全窗口波形图 |
| `RigolDS*_zoom.png` | 可选，`--plot` 生成的双脉冲窗口放大图 |

## 9. 验证结果

本工具包已使用当前实验中的 6 个 Rigol `.bin` 文件完成自检，程序能够生成 `summary.csv`、`analysis_refined.json`、`analysis_report.md` 和可选 PNG 图。包内保留小体积示例输出：

```text
.\sample_output\summary.csv
.\sample_output\analysis_report.md
```

如需复现当前工作区测试，可运行：

```powershell
.\examples\analyze_current_dataset.ps1
```

本程序默认 `--vbus-mode global`，即使用全局高电平平台值作为 Vbus，避免把边沿过冲计入母线电压。因此个别文件和早期手工精细分析会有小差异，但趋势和关键结论一致。若需要按边沿附近局部平台值计算，可使用：

```powershell
--vbus-mode local
```

## 10. 重要限制

1. 如果 CH2 不是 Q1 Vds，必须指定 `--vds-input q2` 或重新测 Q1 Vds。
2. 如果 CH4 不是直接电感电流或 Q1 电流，而是未换算的 CT 电压，必须使用 `ct-voltage` 模式。
3. 当前损耗是工程估算，因为没有直接测 Q1 高带宽漏极电流。
4. SiC 边沿很快，Vds 与电流探头必须做 deskew，否则 Eon/Eoff 会有明显误差。
5. `E_upper` 不是推荐损耗值，只用于上限参考；报告主值使用 `E_linear`。
