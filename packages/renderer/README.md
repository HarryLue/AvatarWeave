# @avatarweave/renderer

AvatarWeave 0.1.0。集成、协议和授权说明见项目 README 与 docs/protocol.md。

口型同步会检测浏览器音频处理能力：支持 AudioWorklet 时使用精细口型处理；HTTP 等不支持的环境使用音频能量驱动基础张合。音频播放保持独立，断开或销毁时释放分析资源，不停止调用方传入的媒体轨道。公网麦克风输入仍需要 HTTPS。
