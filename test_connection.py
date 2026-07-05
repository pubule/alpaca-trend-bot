import sys

import config
from broker import Broker
from notifier import send_telegram_message


def main() -> int:
    config.load_env()
    cfg = config.load_config()

    if not cfg["alpaca"]["paper"]:
        print("FAIL: config.yaml alpaca.paper is not true. Refusing to run test_connection.py against a live account.")
        return 1

    api_key = config.get_required_env("ALPACA_API_KEY")
    api_secret = config.get_required_env("ALPACA_API_SECRET")
    telegram_token = config.get_required_env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = config.get_required_env("TELEGRAM_CHAT_ID")

    broker = Broker(api_key, api_secret, paper=True, data_feed=cfg["alpaca"]["data_feed"])

    ok = True

    try:
        account = broker.get_account()
        print(f"Account equity: {account.equity}, buying power: {account.buying_power}")
    except Exception as e:
        print(f"FAIL: get_account raised {e}")
        ok = False

    order_id = None
    try:
        order = broker.submit_limit_order(
            symbol="AAPL", qty=1, side="buy", limit_price=1.00, tif="gtc"
        )
        order_id = order.id
        print(f"Submitted test limit order {order_id}")
    except Exception as e:
        print(f"FAIL: submit_limit_order raised {e}")
        ok = False

    if order_id is not None:
        try:
            fetched = broker.get_order(order_id)
            print(f"Order status: {fetched.status}")
        except Exception as e:
            print(f"FAIL: get_order raised {e}")
            ok = False

        try:
            broker.cancel_order(order_id)
            print(f"Canceled order {order_id}")
        except Exception as e:
            print(f"FAIL: cancel_order raised {e}")
            ok = False

    telegram_ok = send_telegram_message(
        telegram_token, telegram_chat_id, "test_connection.py: Telegram plumbing OK"
    )
    if telegram_ok:
        print("Telegram test message sent")
    else:
        print("FAIL: Telegram test message not sent")
        ok = False

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
