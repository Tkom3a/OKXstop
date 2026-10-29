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
  
OKXSTOPI HOURLY REPORT  
  
Period: 2026-10-12 11:00 UTC — 2026-10-12 12:00 UTC  
  
Trades: 13  
Volume: 20.35 USDT  
Result: +10$  
Balance change: +0.05 USDT  
  
Balance: 939.64 USDT  
Unrealized PnL: +0.00 USDT  
Open orders: 100  
Open positions: 0  
  
Status: OK  
Last order activity: 5s ago  
Panic events: 0/3  
  
---  
  
OKXSTOPI DAILY REPORT  
  
Period: 2026-10-12 00:00 UTC — 2026-10-13 00:00 UTC  
  
Trades: 40  
Volume: 180.00 USDT  
Result: +18$  
Balance change: +1.05 USDT  
  
Balance: 940.69 USDT  
Unrealized PnL: +0.00 USDT  
Open orders: 100  
Open positions: 0  
  
Status: OK  
Last order activity: 5s ago  
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
  
TRADE CLOSED  
  
BTC-USDT-SWAP  
Amount: 125.40 USDT  
Tokens: 0.0021  
Result: +10$  
  
Balance: 939.64 USDT  
  
---  
  
🚀  
  
---  
  
TRADE CLOSED  
  
ETH-USDT-SWAP  
Amount: 80.00 USDT  
Tokens: 0.03  
Result: -16$  
  
Balance: 923.64 USDT  
  
---  
  
🤡  
  
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
