# OKXSTOP
  
Экстренный стоп-кран для OKX (аварийный выключатель для торговых терминалов, таких как MoonTrader). Скрипт отслеживает изменения лимитных ордеров (переставление), и в случае зависания терминала или падения сервера - закрывает все ордера и открытые позиции.  
  
Также присылает отбивки и статистику в Telegram в формате:  
  
---  
  
🟢 OKXSTOPI STARTED  
  
Balance: 4987.40 USDT  
Orders: 0  
Positions: 0  
Timeout: 120s  
Check interval: 10s  
Panic threshold: 3 (sleep mode after)  
  
---  
  
📊 OKXSTOPI HOURLY REPORT  
  
Period: 2026-06-18 13:47 UTC — 2026-06-18 14:00 UTC  
  
Trades: 0  
Volume: 0.00 USDT  
Realized PnL: +0.00 USDT  
Fees: +0.00 USDT  
Net PnL: +0.00 USDT  
Balance change: +0.00 USDT  
  
💰 Balance: 4987.40 USDT  
Unrealized PnL: +0.00 USDT  
Open orders: 96  
Open positions: 0  
  
Status: ✅ OK  
Last order activity: 0s ago  
Panic events: 0/3  
  
---  
  
🚨 OKXSTOPI PANIC #1  
  
No order activity for 120s  
Last activity: 2026-06-18 13:45 UTC  
  
Orders cancelled: 0  
Positions closed: 0  
Balance: 4987.40 USDT  
Remaining panics until sleep: 2  
  
---  
  
💤 OKXSTOPI ENTERING SLEEP MODE  
  
Panic events: 3 (threshold: 3)  
Watchdog is now sleeping until new trading activity is detected.  
No more panic notifications will be sent.  
  
Current balance: 4987.40 USDT  
  
---  
  
🟢 OKXSTOPI RESUMED  
  
Activity detected - Watchdog is active again!  
  
---  
  
## Установка  
  
git clone https://github.com/Tkom3a/OKXstopi.git  
cd OKXstopi  
Создаем .env файл(шаблон env.example находится  в репозитории проекта) и заполниям все переменные  
  

# Билд и запуск  
  
docker-compose build --no-cache  
docker-compose up -d  
  

# Логи  
  
docker logs -f okxstopi  

# Остановка и удаление  
  
docker-compose down  
docker rmi okxstopi_okxstopi  
