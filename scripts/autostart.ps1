# 设置或取消旁路服务开机自启（Windows 10/11），不需要管理员权限。
# 用法（在 PowerShell 里，于仓库目录运行）：
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1            设置自启
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove    取消自启
# 原理：在当前用户的「启动」文件夹放一个快捷方式，登录 Windows 后最小化运行 scripts\start.bat；
# start.bat 会先等 60 秒，给 SnowLuma 和 QQ 先启动的时间。
param([switch]$Remove)
$ErrorActionPreference = "Stop"

$link = Join-Path ([Environment]::GetFolderPath("Startup")) "幻梦境骰娘旁路服务.lnk"
if ($Remove) {
    if (Test-Path $link) { Remove-Item $link }
    Write-Host "已取消开机自启。"
    return
}

$bat = Join-Path $PSScriptRoot "start.bat"
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$shortcut.TargetPath = $bat
$shortcut.Arguments = "delay"
$shortcut.WorkingDirectory = Split-Path $PSScriptRoot
$shortcut.WindowStyle = 7  # 最小化
$shortcut.Description = "幻梦境骰娘 旁路服务"
$shortcut.Save()
Write-Host "已设置开机自启：登录 Windows 后自动运行 $bat（先等 60 秒）。"
Write-Host "快捷方式位置：$link"
