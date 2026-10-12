# OKXSTOP
  
Экстренный стоп-кран для OKX (аварийный выключатель для торговых терминалов, таких как MoonTrader и MoonBot). Скрипт отслеживает изменения лимитных ордеров (переставление), и в случае зависания терминала или падения сервера - закрывает все ордера и открытые позиции.

С тем же интервалом (`CHECK_INTERVAL`) опрашивает историю закрытых фьючерсных позиций. Когда сделка уже есть в истории, в Telegram уходят только пара, сумма в долларах, профит/убыток, % PnL и в конце баланс торгового счёта (USDT equity).

Та же сводка приходит каждый час и в полночь по `REPORT_TIMEZONE` — полным отчётом за прошедшие сутки. Сделки, закрытые до запуска скрипта, в личные сообщения не дублируются; часовой и суточный отчёты считают только закрытия после старта.
  
Также присылает отбивки и статистику в Telegram в формате:  
  
---  
  
🟢 OKXSTOPI STARTED  
  
Balance: 4987.40 USDT  
Orders: 0  
Positions: 0  
Timeout: 120s  
Check interval: 10s  
Trade alerts: on close, hourly, and daily  
Panic threshold: 3 (sleep mode after)  
  
---  
  
📊 OKXSTOPI HOURLY REPORT  
  
Period: 2026-06-18 18:00 UTC — 2026-06-18 19:00 UTC  
  
BTC-USDT-SWAP LONG  
Amount: 1250.40 USDT  
PnL: +12.35 USDT (+9.88%)  
  
Trades: 1  
Amount: 1250.40 USDT  
PnL: +12.35 USDT (+9.88%)  
  
Status: ✅ OK  
Last order activity: 12s ago  
Open orders: 96  
Open positions: 0  
Panic events: 0/3  
  
💰 Balance: 5012.40 USDT  
  
---  
  
📆 OKXSTOPI DAILY REPORT  
  
Period: 2026-06-18 00:00 UTC — 2026-06-19 00:00 UTC  
  
BTC-USDT-SWAP LONG  
Amount: 1250.40 USDT  
PnL: +12.35 USDT (+9.88%)  
  
ETH-USDT-SWAP SHORT  
Amount: 400.00 USDT  
PnL: -1.20 USDT (-3.00%)  
  
Trades: 2  
Amount: 1650.40 USDT  
PnL: +11.15 USDT (+6.20%)  
  
💰 Balance: 5012.40 USDT  
  
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
  
📈 TRADE CLOSED  
  
BTC-USDT-SWAP LONG  
Amount: 1250.40 USDT  
PnL: +12.35 USDT (+9.88%)  
  
💰 Balance: 5012.40 USDT  
  
---  
  
📈 TRADES CLOSED (2)  
  
BTC-USDT-SWAP LONG  
Amount: 1250.40 USDT  
PnL: +12.35 USDT (+9.88%)  
  
ETH-USDT-SWAP SHORT (partial)  
Amount: 400.00 USDT  
PnL: -1.20 USDT (-3.00%)  
  
💰 Balance: 5012.40 USDT  
  
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
# OKXmbStop
