Set WshShell = CreateObject("WScript.Shell")
' 0 表示隐藏窗口，Chr(34) 是双引号用于处理路径中的空格
WshShell.Run chr(34) & "D:\pythonProject\z办公\钉钉流程\start_dingtalk_stream.bat" & chr(34), 0
Set WshShell = Nothing