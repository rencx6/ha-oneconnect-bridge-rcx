> [!WARNING]
> 本项目是非官方 Home Assistant 自定义集成，未获得华为或 OneConnect 项目方的官方授权、认可或支持。
> 项目用于技术研究和 Home Assistant 实验环境，请遵守相关平台服务条款及适用法律。

> [!NOTE]
> 本仓库是 [xiasi0/ha-oneconnect-bridge](https://github.com/xiasi0/ha-oneconnect-bridge) 的增强 fork（Apache 2.0），
> 在原版基础上新增了「添加实体」配置界面：勾选 HA 实体即可自动生成子设备映射并注册到华为智慧生活，无需手写 YAML。
> 本仓库是[chaosl1996/ha-oneconnect-bridge](https://github.com/chaosl1996/ha-oneconnect-bridge)上适配了自己的产品做了改造。

---

<p align="center">
  <img src="custom_components/oneconnect_bridge/brand/icon.png" alt="OneConnect Bridge" width="160" />
</p>

<h1 align="center">OneConnect Bridge</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Home%20Assistant-Custom%20Integration-41BDF5" alt="Home Assistant Custom Integration" />
</p>

## 项目说明

OneConnect Bridge 是一个运行在 Home Assistant 中、基于 OneConnect 技术栈并对接华为私有业务链路的 Python 网关实现。

它以标准华为父网关身份接入华为智慧生活，再将 HA 实体映射为子设备注册到华为云端；集成负责父网关配对、云端会话、子设备注册、状态同步和上下行控制，HA 侧通过每个网关独立的 YAML 配置选择华为产品模型和服务映射。

本项目的协议实现和业务边界借鉴了 [OneConnect](https://gitcode.com/ohos-oneconnect/communication_iot_connect)、OneConnect 上游文档（[docs](https://gitcode.com/ohos-oneconnect/docs)）、[ha_hwhomebridge](https://github.com/Wangxiaokang666-666/ha_hwhomebridge) C SDK 的真实运行行为，以及 [huawei-smarthome](https://github.com/xiasi0/ha-huawei-smarthome) 的账号侧云端研究结果。

## 前置条件与限制

- Home Assistant：当前按 2026.10.x 版本验证。
- Python：3.14.2+，跟随当前 Home Assistant Core 运行；不支持脱离 Home Assistant 独立运行。
- 局域网：Home Assistant 主机与华为智慧生活 App 应位于可互通的 IPv4 局域网；配置流中必须手动选择可用网卡。
- 配对网络：选定网卡必须允许 IPv4 多播和 UDP 5683 入站/出站，且 UDP 5683 不能被其他进程占用；容器、虚拟机或 VLAN 环境需要额外放行多播。
- 配对并发：同一 Home Assistant 进程同时只允许一个父网关进行局域网配对；已完成绑定的其他网关仍可保持云端在线。
- 实例数量：最多 3 个父网关实例，每个父网关最多 64 个子设备。
- 基于大佬们的研究，跟新了部分接口，增加了部分产品，已测试可用：米家温湿度传感器、米家天然气卫士、向往背景音乐、米家窗帘电机（杜亚）、海尔热水器、海尔洗衣机、海尔冰箱、海尔消毒柜、海尔空调等对应的华为产品

## 快速入门

### 1. 安装

手动安装：

```text
config/custom_components/oneconnect_bridge_rcx/
```

重启 Home Assistant。

### 2. 添加父网关

在 Home Assistant 中选择：

```text
设置 → 设备与服务 → 添加集成 → OneConnect Bridge
```

配置流中手动选择用于局域网配对的 IPv4 网卡。集成会为该实例生成独立的网关序列号，并启动配对监听。

### 3. 完成配对并上线

1. 从 Home Assistant 持久通知中取得 PIN。
2. 在华为智慧生活中添加网关。
3. 手动输入 PIN 完成局域网配对。
4. 等待父网关完成云端注册并显示在线。

父网关上线后，HA 才创建网关实体；此时可以看到在线状态、网关序列号和子设备数量。

### 4. 配置并注册子设备

每个父网关都有独立的运行时配置：

```text
.storage/oneconnect_bridge/gateways/<gateway_serial>/mapping.yaml
```

首次添加网关时，集成会从 [default_mapping.yaml](custom_components/oneconnect_bridge/default_mapping.yaml) 复制样例，默认 `enabled: true`，会加载预置子设备模型。

编辑对应网关的 `mapping.yaml`：

1. 保持 `enabled: true`（如暂不注册子设备，可改为 `false`）；
2. 使用 [catalog.yaml](custom_components/oneconnect_bridge/catalog.yaml) 中存在的产品 `ref`；
3. 填写实际的 HA `entity_id`、服务和字段映射；
4. 点击网关设备上的“重载子设备配置”按钮。

重载成功后，集成会通过父网关会话注册或更新 YAML 中声明的子设备，并开始状态同步和上下行控制。样例中的产品和实体值仅供参考，需要按实际环境替换。

### 5. 添加实体（HomeKit Bridge 式勾选）

无需手写 YAML，即可把任意 HA 实体注册为华为子设备：

```text
设置 → 设备与服务 → OneConnect Bridge → 配置 → 添加实体
```

两步操作：

1. **勾选实体**：支持 `switch` / `light` / `input_boolean` / `input_button` / `script` / `scene` / `lock` / `button` / `automation` / `cover` / `fan` / `humidifier` / `media_player` / `siren` / `valve` / `water_heater`；实体名称自动作为云端设备显示名称；
2. **选择产品样式**：为每个新实体选择华为产品型号（默认「自动」按实体能力选型），型号决定设备在智慧生活中的外观与功能。

- 灯光自动选型：全彩 → RGBCW 驱动（ZG0O），色温 → 调光调色灯（ZG13），亮度 → 调光灯（ZG14），仅开关 → 智能照明（ZG0S）；也可手动指定任意灯类型号（吸顶灯、灯带、射灯等）；
- 开关类实体可选 1/2/3 键产品（ZG2R/ZG2S/ZG2T）；
- `lock` 以智能开关形式包装（开=已上锁，关=已解锁），`script` / `scene` / `button` / `input_button` 以开关形式包装（显示常关，点击触发一次执行/按压），`automation` 以开关包装（开=启用自动化），`media_player` 以开关包装（播放中=开，暂停/空闲=关），`cover` / `fan` / `humidifier` / `siren` / `valve` / `water_heater` 以真实开关状态包装；
- 保存后自动写入对应网关 `mapping.yaml` 并触发子设备重载，取消勾选即删除子设备；
- 自动生成的条目带 `managed_by: entity_bridge` 标记，与手写 children 共存，互不覆盖。

> 只读实体（`sensor` / `binary_sensor`）暂不支持：需要在产品目录中加入上报型传感器品类（温湿度、人体存在等），属于后续真品类实验的范围。

### 6. 重新配对与删除

仅重新启动局域网配对监听，可调用：

```yaml
service: oneconnect_bridge.start_pairing
data:
  serial: <gateway_serial>
```

`start_pairing` 不会删除已有的云端身份。

如果需要删除旧网关后重新配对，请在 HA 中删除整个 OneConnect Bridge 集成。集成会依次清理子设备、revoke 父网关并删除对应持久化目录，然后重新添加集成即可。

## 相关项目

- [OneConnect](https://gitcode.com/ohos-oneconnect/communication_iot_connect) 上游 C SDK
- OneConnect 上游文档（[docs](https://gitcode.com/ohos-oneconnect/docs)）
- [ha_hwhomebridge](https://github.com/Wangxiaokang666-666/ha_hwhomebridge)
- [huawei-smarthome](https://github.com/xiasi0/ha-huawei-smarthome)

## 许可

Apache License 2.0，详见仓库根目录的 [LICENSE](LICENSE)。
