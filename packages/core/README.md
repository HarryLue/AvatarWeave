# @avatarweave/core

AvatarWeave 0.1.0。集成、协议和授权说明见项目 README 与 docs/protocol.md。

`dispose()` 可重复调用，并发调用等待同一次销毁完成；传入外部 Room 时仍由宿主管理连接。SDK 自建房间的主动退出 1006 日志按调试信息处理，活动连接的意外断线仍保留错误。已销毁客户端的迟到转写不会继续触发 `onError`。
