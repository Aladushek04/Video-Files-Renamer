$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Get-Python {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py"
    }
    throw "Python не найден. Установите Python 3.10+ и добавьте его в PATH."
}

$python = Get-Python

function Invoke-Cli {
    param([string[]]$CliArgs)
    # Вывод CLI идёт сразу в консоль: в success stream должен попасть только код.
    # Иначе `$code = Invoke-Cli ...` получает массив строк вместе с кодом,
    # и `$code -ne 0` выглядит правдой при успешном вызове.
    if ($python -eq "py") {
        & py -3 -m video_renamer.cli @CliArgs | Out-Host
    } else {
        & python -m video_renamer.cli @CliArgs | Out-Host
    }
    return $LASTEXITCODE
}

function Wait-Enter {
    param([string]$Prompt = "Нажмите Enter")
    Read-Host $Prompt | Out-Null
}

function Open-Path {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    try {
        Start-Process -FilePath $Path -ErrorAction Stop | Out-Null
    } catch {
        Write-Host "Файл: $Path"
    }
}

function Select-Folder {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "Выберите папку с видео"
    $dialog.ShowNewFolderButton = $false
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.SelectedPath
    }
    return $null
}

function Invoke-Preview {
    param([string]$Folder)
    $code = Invoke-Cli @("preview", $Folder, "--csv", "preview.csv")
    if ($code -ne 0) {
        Write-Host "Ошибка preview (код $code)."
        Wait-Enter "Нажмите Enter, чтобы вернуться в меню"
        return $false
    }
    Write-Host "Готово: preview.csv"
    Open-Path (Join-Path $projectRoot "preview.csv")
    return $true
}

function Invoke-Apply {
    param([string]$Folder)
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "preview.csv"))) {
        Write-Host "preview.csv не найден. Сначала сделайте preview."
        Wait-Enter "Нажмите Enter"
        return
    }
    Write-Host "Применяю только строки с approved=y..."
    $code = Invoke-Cli @("apply", "preview.csv", "--root", $Folder, "--apply")
    if ($code -eq 0) {
        Write-Host "Готово. Журнал: rename_log.csv"
        Open-Path (Join-Path $projectRoot "rename_log.csv")
    } else {
        Write-Host "Ошибка apply (код $code)."
    }
    Wait-Enter "Нажмите Enter, чтобы вернуться в меню"
}

function Invoke-Main {
    param([string[]]$Arguments)

    $folder = $null
    if ($Arguments.Count -gt 0) {
        $folder = $Arguments[0]
    } else {
        $folder = Select-Folder
    }
    if (-not $folder) {
        Write-Host "Папка не выбрана."
        Wait-Enter "Нажмите Enter"
        exit 1
    }
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
        Write-Host "Папка не найдена: $folder"
        Wait-Enter "Нажмите Enter"
        exit 1
    }

    while ($true) {
        Clear-Host
        Write-Host "============================================"
        Write-Host "  Video Files Renamer"
        Write-Host "============================================"
        Write-Host "  Папка: $folder"
        Write-Host ""
        Write-Host "  1. Preview — показать предложения (файлы не меняются)"
        Write-Host "  2. Preview + применить после ревью"
        Write-Host "  3. Применить уже готовый preview.csv"
        Write-Host "  0. Выход"
        Write-Host ""
        $choice = Read-Host "Выберите пункт"

        switch ($choice) {
            "1" {
                if (Invoke-Preview $folder) {
                    Read-Host "Проверьте CSV. Enter — вернуться в меню"
                }
            }
            "2" {
                if (Invoke-Preview $folder) {
                    Write-Host ""
                    Write-Host "Поставьте approved=y для нужных строк, сохраните и закройте Excel."
                    Read-Host "Enter — применить переименование"
                    Invoke-Apply $folder
                }
            }
            "3" {
                Invoke-Apply $folder
            }
            "0" {
                exit 0
            }
            default {
                Write-Host "Нет такого пункта."
                Start-Sleep -Seconds 1
            }
        }
    }
}

# Тесты загружают функции через dot-sourcing и не должны попадать в меню.
if (-not $env:VIDEO_RENAMER_PS_NO_MAIN) {
    Invoke-Main $args
}
