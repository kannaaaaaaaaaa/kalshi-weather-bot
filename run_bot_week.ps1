# PowerShell script to run the Kalshi weather bot continuously for a week
# Usage: .\run_bot_week.ps1

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "  Kalshi Weather Bot - Week-long Paper Trading Run" -ForegroundColor White
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan

# Check if virtual environment exists
if (-Not (Test-Path "venv\Scripts\Activate.ps1")) {
    Write-Host ""
    Write-Host "ERROR: Virtual environment not found!" -ForegroundColor Red
    Write-Host "   Please create it first:" -ForegroundColor Yellow
    Write-Host "   python -m venv venv" -ForegroundColor Yellow
    Write-Host "   .\venv\Scripts\Activate.ps1" -ForegroundColor Yellow
    Write-Host "   pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# Activate virtual environment
Write-Host ""
Write-Host "Activating virtual environment..." -ForegroundColor Cyan
& .\venv\Scripts\Activate.ps1

# Create logs directory
if (-Not (Test-Path "logs")) {
    New-Item -ItemType Directory -Path "logs" | Out-Null
}

$timestamp = (Get-Date).ToString("yyyy-MM-dd_HH-mm-ss")
$logFile = "logs\bot_run_$timestamp.log"

Write-Host "Starting bot..." -ForegroundColor Cyan
Write-Host "   Log file: $logFile" -ForegroundColor Gray
Write-Host "   Database: data\weather_bot.db" -ForegroundColor Gray
Write-Host "   Bot will run continuously. Press Ctrl+C to stop." -ForegroundColor Yellow

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

# Run the bot, writing to console and log file simultaneously
python main.py 2>&1 | Tee-Object -FilePath $logFile -Append

Write-Host ""
Write-Host "Bot stopped." -ForegroundColor Green
Write-Host "   View results with: python view_results.py" -ForegroundColor Cyan
