<#
.SYNOPSIS
Dumps the Windows UI Automation tree of a window by title.

.DESCRIPTION
Diagnostic helper for accessibility checks. It does not change the app:
it only enumerates automation elements, their names, roles, focusability and
enabled state. Use it to verify what Narrator/NVDA can see.

.EXAMPLE
powershell -NoProfile -File scripts\check_uia.ps1 "Video Files Renamer"
#>
param(
    [string]$Title = "Video Files Renamer"
)

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$root = [System.Windows.Automation.AutomationElement]::RootElement
$condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty,
    $Title
)
$window = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $condition)
if (-not $window) {
    Write-Error "Window not found: $Title"
    exit 1
}

$elements = $window.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)
Write-Output "Descendants: $($elements.Count)"
foreach ($element in $elements) {
    $current = $element.Current
    Write-Output (
        "{0} | name='{1}' | class={2} | focus={3} | enabled={4}" -f `
            $current.ControlType.ProgrammaticName,
            $current.Name,
            $current.ClassName,
            $current.IsKeyboardFocusable,
            $current.IsEnabled
    )
}
