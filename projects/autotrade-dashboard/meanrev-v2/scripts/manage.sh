#!/bin/bash
# Usage: ./scripts/manage.sh [start|stop|restart|logs|status|update]
APP="meanrev-v2"
case ${1:-status} in
  start)    pm2 start ecosystem.config.js ;;
  stop)     pm2 stop $APP ;;
  restart)  pm2 restart $APP ;;
  logs)     pm2 logs $APP --lines 200 ;;
  status)   pm2 status $APP ;;
  update)   git pull 2>/dev/null||true; npm install --production; pm2 restart $APP ;;
  *)        echo "Usage: $0 [start|stop|restart|logs|status|update]" ;;
esac
