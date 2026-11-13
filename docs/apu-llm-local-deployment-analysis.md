# APU 本地部署大模型可行性分析报告

> 编写时间：2026-07-15
> 目标场景：单人工作站上本地运行 7B~70B 量级开源大模型（LLM），用于开发/调试/推理
> 当前基线：HP Z6 G4 (Xeon Silver 4210 10C/20T, 32GB DDR4, Quadro P2200 5GB)

---

## 1. 核心结论（TL;DR）

| 场景 | APU 可行性 | 推荐方案 | 预期性能 |
|------|-----------|---------|---------|
| 7B Q4 (~4GB) | ✅ 完全可行 | 8700G + 64GB DDR5-6400 | 15-25 tok/s |
| 13B Q4 (~8GB) | ✅ 完全可行 | 8700G + 64GB DDR5-6400 | 8-12 tok/s |
| 34B Q4 (~19GB) | ⚠️ 勉强可用 | 8700G + 96GB DDR5-6400 | 3-5 tok/s |
| 70B Q4 (~39GB) | ⚠️ 能用但慢 | Strix Point + 96GB | 1.5-3 tok/s |
| 120B+ Q4 (~60+GB) | ❌ 不推荐 | 需要多路/Apple Silicon | <1 tok/s |

**一句话判断：** APU 方案的优势是**能在大显存（96GB DDR5）下跑大模型**，但速度远低于同价位独显方案。如果经常跑 13B 以下模型，加一张 RTX 4060 Ti 16GB (¥3500) 秒杀任何 APU。只有当你需要跑 34B+ 大模型、且接受 1-5 tok/s 的速度时，APU 方案才有意义。

---

## 2. 当前硬件瓶颈分析

你现有的 HP Z6 G4 工作站跑大模型的主要瓶颈：

### 2.1 GPU 瓶颈（最严重）

| 项目 | 当前值 | 大模型最低需求 | 差距 |
|------|-------|---------------|------|
| Quadro P2200 VRAM | **5 GB** | 7B Q4 > 4GB；13B Q4 > 8GB | ⛔ 只能跑 7B Q4，极限 |
| 显存带宽 | **200 GB/s** | 理想 > 500 GB/s | 够用但不是瓶颈主因 |
| 架构 | Pascal (2016) | 无 INT8/FP8 加速 | ⛔ 推理效率低 |
| CUDA 能力 | 6.1 | 8.0+ 有 Transformer Engine | ⛔ 不支持 Flash Attention 优化 |

P2200 的 5GB 是硬天花板——能跑的最大模型是 7B Q4_K_M（~4.5GB），再多一点就 OOM。34B/70B 完全不可能。

### 2.2 CPU 瓶颈

| 项目 | 当前值 | 说明 |
|------|-------|------|
| Xeon Silver 4210 | 10C/20T, 2.2GHz base | 纯 CPU 推理可跑任何模型（只要有 RAM） |
| 内存通道 | **6通道 DDR4-2400** | 峰值带宽 ~115 GB/s，比消费级双通道还高 |
| L3 缓存 | 27.5MB | 对 CPU 推理有一定帮助 |

有趣的是，Z6 G4 的 **6通道内存** 是亮点——CPU 推理带宽达到 ~115 GB/s，比普通消费级双通道 DDR5 (~90 GB/s) 更高。这意味着纯 CPU 推理可能比 APU 方案更快。

### 2.3 当前能跑什么

使用 llama.cpp + CPU 推理（目前的可行方案）：

| 模型 | 量化 | 内存需求 | 能否跑 | 预估速度 |
|------|------|---------|--------|---------|
| Qwen2.5-7B | Q4_K_M | ~5GB | ✅ 可 | 20-30 tok/s |
| Qwen2.5-14B | Q4_K_M | ~8.5GB | ✅ 可 | 10-15 tok/s |
| DeepSeek-V2-Lite-16B | Q4_K_M | ~10GB | ✅ 可 | 8-12 tok/s |
| Yi-34B | Q4_K_M | ~19GB | ❌ 超 RAM | - |
| Llama-3-70B | Q4_K_M | ~39GB | ❌ 超 RAM | - |

**32GB RAM 限制了 CPU 推理的上限在 ~20B 模型。** 加内存条到 64GB 可以在 CPU 上跑 34B 模型（10-15 tok/s，因为 6通道带宽还不错）。

---

## 3. APU 技术分析

### 3.1 什么是 APU（当前产品线）

AMD 把高性能集成显卡（RDNA 3/3.5）和 Zen 4/5 CPU 封装在同一颗芯片上，统称 APU。当前在售的产品线：

| 系列 | 型号 | CPU | GPU | CUs | 内存 | 平台 | 典型设备 |
|------|------|-----|-----|-----|------|------|---------|
| **Ryzen 8000G** | 8700G | 8C Zen4 | R780M | 12 CU RDNA3 | DDR5 | AM5 桌面 | DIY 装机 |
| | 8600G | 6C Zen4 | R760M | 8 CU RDNA3 | DDR5 | AM5 桌面 | DIY 装机 |
| | 8500G | 6C Zen4 | R740M | 4 CU RDNA3 | DDR5 | AM5 桌面 | 不推荐 |
| **Ryzen AI 300** | HX 370 | 12C Zen5 | R890M | 16 CU RDNA3.5 | LPDDR5X | FP8 移动 | Minisforum UM890 Pro |
| | 365 | 10C Zen5 | R880M | 12 CU RDNA3.5 | LPDDR5X | FP8 移动 | AI 迷你主机 |
| **Ryzen 7040/8040** | 7840U/8840U | 8C Zen4 | R780M | 12 CU RDNA3 | LPDDR5X | FP7/FP8 | 迷你主机/笔记本 |
| | 7940HS/8945HS | 8C Zen4 | R780M | 12 CU RDNA3 | DDR5/LPDDR5X | 笔记本 | 游戏本/迷你主机 |

**Radeon 780M** 和 **Radeon 890M** 是当前 APU 集显性能天花板。R890M 比 R780M 多 33% CU（16 vs 12），架构升级到 RDNA 3.5。

### 3.2 APU 推理大模型的关键约束

#### 3.2.1 显存 = 系统内存（共享）

APU 没有独立 VRAM——GPU 和 CPU 共享 DDR5 内存。这是最大的双刃剑：

- **优势**：可分配的「显存」远大于消费级独显——96GB 起步，128GB 也可行
- **劣势**：GPU 和 CPU 争抢同一带宽，且 DDR5 带宽远低于 GDDR

```text
带宽对比（GB/s）：
  DDR5-4800 双通道  |  76.8 GB/s
  DDR5-5600 双通道  |  89.6 GB/s
  DDR5-6400 双通道  |  102.4 GB/s      ← 高端 APU 的上限
  DDR5-8000 双通道  |  128.0 GB/s      ← 需要 CPU IMC 和主板都支持
  ─────────────────────────────────────
  LPDDR5X-7500 四通道|  120.0 GB/s     ← Strix Point 移动平台
  ─────────────────────────────────────
  RTX 3060 GDDR6     |  360.0 GB/s     ← 入门独显
  RTX 3090 GDDR6X    |  936.2 GB/s     ← 二手性价比之王
  RTX 4090 GDDR6X    |  1,008 GB/s     ← 旗舰
  Apple M2 Ultra HBM |  800.0 GB/s     ← 统一内存天花板
  Apple M3 Max LPDDR |  400.0 GB/s     ← 统一内存次优
```

**DDR5 带宽是最大的性能瓶颈。** APU iGPU 本身算力足够（R780M ~8.6 TFLOPS FP16），但数据在显存和计算单元之间的传输速度决定了推理速度。

#### 3.2.2 ROCm 软件生态

AMD 的 GPU 计算平台 ROCm 对 RDNA 3 APU 的支持情况：

| ROCm 版本 | APU 支持状态 | 说明 |
|-----------|-------------|------|
| ROCm 5.x | ❌ 不支持 | 仅支持 CDNA/Fiji/Vega |
| ROCm 6.0 | ⚠️ 实验性 | RDNA 3 dGPU 支持，APU 未正式列入 |
| ROCm 6.1+ | ⚠️ 部分 | 增加了对 Phoenix APU 的实验性支持 |
| ROCm 6.2+ | ⚠️ 改进中 | 更多 RDNA 3 APU 加入，但仍需环境变量启用 |

**实际建议**：不要依赖 ROCm 官方支持。用 llama.cpp 的 **Vulkan 后端** 或 **HIP（需手动编译）** 来利用 iGPU。

#### 3.2.3 推理速度公式

LLM 推理的核心瓶颈是**显存带宽**。给定模型大小和带宽，预估 token 生成速度：

```
推理速度 ≈ 显存带宽 × 利用率 / 模型参数量 × 量化位数
```

**示例计算**（假定 DDR5-6400 双通道，102.4 GB/s，利用率 70%）：

| 模型 | 大小 | 带宽瓶颈速度 | 实际体验 |
|------|------|------------|---------|
| 7B Q4_K_M | ~4.5 GB | 102.4×0.7/4.5 ≈ 16 tok/s | 流畅阅读 |
| 13B Q4_K_M | ~8 GB | 102.4×0.7/8 ≈ 9 tok/s | 可接受聊天 |
| 34B Q4_K_M | ~19 GB | 102.4×0.7/19 ≈ 3.8 tok/s | 逐字输出 |
| 70B Q4_K_M | ~39 GB | 102.4×0.7/39 ≈ 1.8 tok/s | 卡顿明显 |

> 注：以上是纯 GPU 推理的极限估算。实际使用 llama.cpp 的 GPU offload 模式时，只有部分层跑在 GPU，其余在 CPU，速度介于 GPU-only 和 CPU-only 之间。

### 3.3 APU vs 独显 vs Apple Silicon 交叉对比

| 指标 | APU (8700G+96GB) | RTX 4060 Ti 16GB | RTX 3090 24GB (二手) | Apple M2 Ultra |
|------|------------------|-----------------|--------------------|----------------|
| 价格 | ~¥6,000 | ~¥3,500 | ~¥5,000(二手) | ~¥40,000+ |
| 最大模型(4bit) | 70B+ | 13B-16B | 34B-40B | 120B+ |
| 7B 速度 | 16 tok/s | 80+ tok/s | 150+ tok/s | 100+ tok/s |
| 13B 速度 | 9 tok/s | 45+ tok/s | 80+ tok/s | 60+ tok/s |
| 34B 速度 | 4 tok/s | ❌ 放不下 | 30+ tok/s | 25+ tok/s |
| 70B 速度 | 1.8 tok/s | ❌ 放不下 | ❌ 放不下 | 12+ tok/s |
| 扩展性 | 换内存条可升级 | 固定 16GB | 固定 24GB | 固定(买断) |
| 功耗 | ~150W 满载 | ~160W + CPU | ~350W + CPU | ~60W |
| 软件生态 | ⚠️ Vulkan/HIP | ✅ CUDA 生态 | ✅ CUDA 生态 | ✅ MLX/llama.cpp |
| 多任务 | 共享内存带宽 | 独立显存 | 独立显存 | 统一内存 |

---

## 4. 推荐 APU 配置方案

### 方案 A：AM5 桌面 APU「高内存」方案（推荐）

🚀 **最适合本地大模型开发的 APU 方案，性价比优先**

#### 配置清单

| 部件 | 型号 | 预算（¥） | 备注 |
|------|------|----------|------|
| **CPU** | AMD Ryzen 7 **8700G** | ¥1,800 | 8C/16T Zen4, R780M 12CU |
| **主板** | 华硕 TUF GAMING **B650M-E** | ¥900 | PCIe 4.0, DDR5 双通道 |
| **内存** | 金士顿 Fury **64GB (2×32GB) DDR5-6400** CL32 | ¥1,600 | **关键部件**，带宽越高越好 |
| **SSD** | 致态 TiPlus 7100 **2TB** | ¥900 | PCIe 4.0, 够用 |
| **电源** | 长城 V6 **650W** 金牌直出 | ¥400 | 额定功率充足 |
| **机箱** | 先马 趣造 **mATX** | ¥300 | 紧凑便携 |
| **散热** | 利民 AX120 R SE | ¥100 | 8700G 功耗不高 |
| **合计** | | **¥6,000** | |

#### 可选升级

| 升级项 | 成本 | 收益 |
|-------|------|------|
| 内存升级到 **96GB (48GB×2)** DDR5-6400 | +¥1,600 | 可跑 70B Q4 |
| 内存升级到 **128GB (64GB×2)** DDR5-5600 | +¥3,000 | 可跑 120B Q3 (~70GB) |
| 主板升级到 **B650E** | +¥600 | 更好的 PCIe 5.0 |
| 加装 RTX 4060 Ti **16GB** 双卡模式 | +¥3,500 | iGPU 跑小模型 + dGPU 跑大模型 |

#### 预期性能

| 配置 | 7B Q4 | 13B Q4 | 34B Q4 | 70B Q4 |
|------|-------|--------|--------|--------|
| 8700G + DDR5-6400 64GB（单 iGPU） | 16-20 tok/s | 9-12 tok/s | 3-5 tok/s | ❌ 放不下 |
| 8700G + DDR5-6400 96GB（单 iGPU） | 同上 | 同上 | 同上 | 1.5-3 tok/s |
| 8700G + DDR5-8000 64GB（单 iGPU） | 22-28 tok/s | 12-15 tok/s | 5-6 tok/s | ❌ 放不下 |
| CPU-only（Xeon 4210 + 6通道 DDR4） | 20-30 tok/s | 10-15 tok/s | ❌ 32GB不够 | ❌ |

**有意思的结论：** 8700G 的 iGPU 推理 7B/13B 并不比你的 Xeon CPU 推理快多少，因为内存带宽相近（8700G 双通道 DDR5-6400 ~102 GB/s vs Xeon 6通道 DDR4-2400 ~115 GB/s）。但 APU 的优势在于 **可以跑更大的模型**（96GB 内存装上后跑 34B/70B）。

### 方案 B：迷你主机「省心省地」方案

🤖 **即买即用，不折腾**

#### 推荐设备

| 型号 | CPU | GPU | 内存上限 | 价格 | 特点 |
|------|-----|-----|---------|------|------|
| **Minisforum UM890 Pro** | Ryzen AI 9 HX 370 | Radeon 890M (16CU) | **96GB LPDDR5X** | ¥6,500 | 性能最强迷你APU |
| **Minisforum EliteMini AI370** | Ryzen AI 9 HX 370 | Radeon 890M (16CU) | 64GB LPDDR5X | ¥5,500 | 性价比 |
| **ASUS NUC 14 Pro+** | Ryzen AI 9 HX 370 | Radeon 890M (16CU) | 96GB LPDDR5X | ¥7,500 | 品牌售后好 |
| **GPD Win Max 2 (2025)** | Ryzen AI 9 HX 370 | Radeon 890M (16CU) | 96GB LPDDR5X | ¥8,000 | 便携+手柄 |

#### 预期性能（UM890 Pro）

- 7B Q4: ~18-25 tok/s（R890M 比 R780M 快约 20%）
- 13B Q4: ~10-14 tok/s
- 34B Q4: ~4-6 tok/s（96GB 版本）
- 70B Q4: ~2-3 tok/s（96GB 版本）

**优点**：体积小（~0.5L），功耗低（~65W），即插即用
**缺点**：升级空间小、散热受限、价格稍高

### 方案 C：双机协作「保留现有工作站」方案

🔄 **当前工作站 + APU 推理节点**

不放弃现有 Z6 G4，把 APU 作为专用推理服务器：

| 角色 | 设备 | 职责 |
|------|------|------|
| 开发机 | HP Z6 G4 (现有) | 日常编码、RID 项目、llama.cpp CPU 推理小模型 |
| 推理机 | UM890 Pro / 8700G 小主机 | 跑 34B-70B 大模型、API 服务 |

**优点**：
- 现有投资不浪费（Xeon CPU 跑 7B/13B 够了）
- 推理机可以放在角落当 API server
- RID 项目开发和 LLM 推理互不干扰

**缺点**：
- 总成本 ¥6,000~8,000
- 占用两个电源插口和空间

---

## 5. 软件部署可行性

### 5.1 llama.cpp（最推荐的推理引擎）

llama.cpp 支持 APU iGPU 的三种后端：

#### 方案 1：Vulkan 后端（推荐，最稳定）

```bash
# 编译带 Vulkan 支持的 llama.cpp
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp
cmake -B build -DGGML_VULKAN=ON
cmake --build build --config Release -j

# 运行，-ngl N 把 N 层卸载到 iGPU
./build/bin/llama-cli -m Qwen2.5-14B-Q4_K_M.gguf \
  -ngl 40 \
  -n 512 \
  -p "你好，请介绍一下自己"
```

**性能调优关键参数：**
- `-ngl N`：卸载到 GPU 的层数。对于 8700G 的 R780M，建议 30-80%（看模型大小）
- `-t N`：CPU 线程数，建议 4-8
- `-ub 1024`：batch size，调大可提升 GPU 利用率
- `--no-mmap`：在共享内存场景下有时更快

#### 方案 2：HIP 后端（ROCm，实验性）

```bash
# 需要先安装 ROCm（仅部分 APU 支持）
# 在 Ubuntu 24.04 上安装 ROCm 6.2+
sudo apt update
sudo apt install rocm-hip-libraries rocm-dev hip-dev

# 编译 llama.cpp 带 HIP 支持
cmake -B build -DGGML_HIP=ON -DAMDGPU_TARGETS="gfx1103"  # RDNA 3 APU
cmake --build build --config Release -j
```

> ⚠️ gfx1103 是 RDNA 3 APU 的 target ID（Phoenix/Hawk Point）。RDNA 3.5（Strix Point）可能用 gfx1150/1151——编译前需确认。

#### 方案 3：SYCL 后端（Intel CPU + AMD GPU 混合）

如果系统里同时有 Intel CPU 和 AMD APU，SYCL 可以统一调度：

```bash
cmake -B build -DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx
```

### 5.2 Ollama（一键部署，易用性优先）

```bash
# 安装 Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 设置环境变量启用 Vulkan
export OLLAMA_LLM_LIBRARY=llama_vulkan

# 拉取并运行模型
ollama pull qwen2.5:14b-q4_K_M
ollama run qwen2.5:14b-q4_K_M
```

Ollama 会自动检测 Vulkan 设备并使用 iGPU 加速。

### 5.3 混合推理建议：分模型策略

正确的分配方式不是只用 iGPU 或 CPU，而是**按层混合**：

```
# 对于 34B 模型，70% 层 iGPU + 30% 层 CPU 是最优配置
./llama-cli -m Yi-34B-Q4_K_M.gguf \
  -ngl 50 \          # 50 层（~70%）卸载到 iGPU
  -t 6 \             # CPU 用 6 个线程跑剩余层
  -c 2048            # context 长度
```

经验法则：**让 iGPU 处理注意力层（计算密集），CPU 处理 FFN 层的前几层和后几层（带宽敏感）**。

---

## 6. 关键局限与风险

### 6.1 内存带宽是硬天花板

无论 APU 的 iGPU 算力多强，**DDR5 带宽决定了推理速度上限**。这是物理定律（冯·诺依曼瓶颈），无法通过软件优化绕过。

```text
DDR5-6400 (102 GB/s) vs GDDR6X (936 GB/s)
→ APU 推理理论极限只有 RTX 3090 的 11%
```

### 6.2 iGPU + CPU 推理不如纯 CPU 推理快的情况

当模型较小（≤13B Q4）时，APU 的 iGPU 卸载优势不明显：

| 场景 | 纯 CPU (Xeon 6ch) | APU iGPU (8700G) | dGPU (RTX 3090) |
|------|-------------------|------------------|----------------|
| 7B Q4 | 20-30 tok/s | 16-20 tok/s | 150+ tok/s |
| 13B Q4 | 10-15 tok/s | 9-12 tok/s | 80+ tok/s |

**因为 Xeon 的 6 通道 DDR4 带宽（115 GB/s）比 8700G 的双通道 DDR5（102 GB/s）更高。** APU 的共享内存架构在带宽上并不比服务器 CPU 有优势。

### 6.3 ROCm 支持不完善

- RDNA 3 APU 的 ROCm 支持仍处于"可用但不稳定"状态
- 每次 ROCm 版本升级都可能 break
- 用 Vulkan 后端更稳定，但性能比 HIP 低 10-20%
- Strix Point (RDNA 3.5) 的 ROCm 支持可能要 2026 年底才稳定

### 6.4 多任务时带宽竞争

运行大模型推理时 iGPU 会占满内存带宽。同时做其他操作（编译代码、打开浏览器）会：

- 推理速度进一步下降
- 系统响应变卡
- 极端情况可能导致推理 OOM

**解决办法**：用 `nice -n 19` 降低推理进程优先级，或使用方案 C（独立推理机）。

---

## 7. 替代方案全面对比

### 7.1 不买 APU，升级现有工作站

如果你的预算是 ¥6,000，APU 不是唯一选择：

| 方案 | 成本 | 最大模型 | 7B 速度 | 70B 速度 | 评价 |
|------|------|---------|---------|---------|------|
| **APU 方案 A** (8700G 新装机) | ¥6,000 | 70B Q4 | 16-20 | 1.5-3 | ⭐ 新平台、内存大 |
| **给 Z6 加 64GB 内存** | ¥800 | 34B Q4 | 20-30(CPU) | ❌ | 最便宜、立即生效 |
| **给 Z6 加 RTX 4060 Ti 16GB** | ¥3,500 | 13B Q4 | 80+ | ❌ 放不下 | CUDA 生态、速度快 |
| **给 Z6 加 RTX 3090 24GB 二手** | ¥5,000 | 34B Q3 | 150+ | ❌ 放不下 | ⭐ 性价比王、需换电源 |
| **APU 方案 B** (UM890 Pro) | ¥6,500 | 70B Q4 | 18-25 | 2-3 | 省心省地 |
| **Mac Mini M4 Pro + 48GB** | ¥12,000 | 34B Q4 | 80+ | ❌ | AI 开发不错但贵 |
| **Mac Studio M2 Ultra + 96GB** | ¥30,000+ | 120B Q4 | 100+ | 12+ | 终极但太贵 |

### 7.2 关键建议：先加内存试试！

**¥800 升级**：给 Z6 G4 加 64GB DDR4 ECC 内存条（现有是 6通道 32GB 即 6×？GB）。如果主板有空槽，加几条到 128GB，你可以在 CPU 上跑 34B~70B 模型（虽然慢但不用换机）。

```bash
# 确认内存槽使用情况
sudo dmidecode -t memory | grep -E "Locator|Size|Speed" | head -20
```

### 7.3 APU 最适合的用户画像

APU 方案真正合适的场景：

1. ✅ **需要本地跑 34B-70B 大模型**，但预算有限（¥6000-8000）
2. ✅ **需要紧凑/便携**（迷你主机放桌上不占地方）
3. ✅ **兼顾日常轻度 GPU 任务**（集成显卡能打游戏、看视频、AI 画图）
4. ✅ **对推理速度不敏感**（能接受 2-5 tok/s 的逐字输出）
5. ⚠️ **CUDA 生态不依赖**（项目本身用 llama.cpp/Vulkan）

APU **不**适合的场景：

1. ❌ **主要跑 7B-13B 小模型** → 加一张 RTX 4060 Ti 16GB 更快更便宜
2. ❌ **需要高推理吞吐**（API server、批量处理）→ 必须上独显
3. ❌ **炼丹/微调训练** → APU 的 ROCm 支持不够；需要 CUDA
4. ❌ **3D 渲染/CAD** → 独显的 NVENC/CUDA 核心更合适

---

## 8. 最终建议

### 分阶段升级路径

```
阶段 0（¥0）：先用 Xeon CPU 跑 llama.cpp 小模型，验证你的工作流
  ↓
阶段 1（¥800）：给 Z6 加 64GB DDR4 → 可以跑 34B CPU 推理
  ↓
阶段 2（¥3,500）：给 Z6 加 RTX 4060 Ti 16GB → CUDA 加速小模型
  ↓
阶段 3（¥6,000）：装 APU 方案 A (8700G+96GB) → 可以跑 70B 大模型
  ↓
阶段 4（¥9,000+ 总）：Z6 做开发和日常推理 + APU 做 70B API Server
```

### 不走弯路的核心建议

1. **APU 不是 LLM 的最佳选择，但确实提供了「便宜的大显存」这条路径**
2. 如果主要跑 ≤13B 模型，**加一张独显比 APU 方案好得多**
3. 如果主要跑 34B+，**APU 方案是预算最友好的大显存方案**
4. **不要高估 APU 的推理速度**——它和 CPU 推理速度相当，远低于独显
5. **ROCm 的现状决定了**：目前 APU 上跑 LLM 的最佳实践是 llama.cpp + Vulkan 后端

---

## 附录

### A. 模型大小速查表（4-bit 量化）

| 模型 | 参数量 | Q4_K_M 大小 | 需要 RAM |
|------|-------|------------|---------|
| Qwen2.5-0.5B | 0.5B | ~0.4 GB | 2 GB |
| Qwen2.5-1.5B | 1.5B | ~1.0 GB | 4 GB |
| Qwen2.5-7B | 7B | ~4.5 GB | 8 GB |
| DeepSeek-V2-Lite | 16B | ~9.8 GB | 16 GB |
| Qwen2.5-14B | 14B | ~8.5 GB | 16 GB |
| Yi-34B | 34B | ~19.5 GB | 32 GB |
| Qwen2.5-32B | 32B | ~18.0 GB | 32 GB |
| Llama-3-70B | 70B | ~39.5 GB | 64 GB |
| DeepSeek-V2 | 236B MoE | ~84 GB (Q2) | 128 GB |
| Llama-3.1-405B | 405B | ~220 GB (Q3) | 384 GB |

### B. APU iGPU 对 ROCm 的支持 Target ID

| GPU | 架构 | ROCm Target ID | 状态 |
|-----|------|---------------|------|
| Radeon 780M (Phoenix) | RDNA 3 | `gfx1103` | ⚠️ ROCm 6.2+ 实验性 |
| Radeon 760M (Phoenix) | RDNA 3 | `gfx1103` | ⚠️ 同上 |
| Radeon 740M (Phoenix) | RDNA 3 | `gfx1103` | ⚠️ 同上 |
| Radeon 890M (Strix Point) | RDNA 3.5 | `gfx1150` | ❌ 尚不支持 |
| Radeon 880M (Strix Point) | RDNA 3.5 | `gfx1150` | ❌ 尚不支持 |

### C. 验证 APU 推理性能的命令

```bash
# 1. 检查 iGPU 是否被正确识别
lspci | grep -i "VGA\|Display"
# 应该显示类似：AMD Phoenix/Strix Point [Radeon 780M/890M]

# 2. 检查 ROCm 是否检测到 GPU
rocminfo | grep -A 3 "Name.*GPU"

# 3. 检查 Vulkan 设备
vulkaninfo | grep -A 10 "Device Name"

# 4. 基准测试：llama.cpp 的内置 bench
./build/bin/llama-bench -m model.gguf -ngl 40

# 5. 实时监控 GPU 使用
sudo apt install radeontop
sudo radeontop  # 查看 iGPU 利用率
```

### D. 参考文献

1. AMD ROCm 文档：https://rocm.docs.amd.com
2. llama.cpp Vulkan 后端：https://github.com/ggerganov/llama.cpp#vulkan
3. llama.cpp AMD GPU 指南：https://github.com/ggerganov/llama.cpp/wiki/Build.md#amd-gpu
4. Ollama 官方文档：https://github.com/ollama/ollama
5. AMD Ryzen 8000G 系列规格：https://www.amd.com/en/products/processors/desktops/ryzen/8000-series/
6. Minisforum UM890 Pro：https://store.minisforum.com/products/um890-pro
