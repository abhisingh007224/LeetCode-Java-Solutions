from app.commons.runtime import runtime


def stripe_payment_intent_webhook_event_enabled() -> bool:
    return runtime.get_bool(
        "enable_payin_stripe_payment_intent_webhook_enabled.bool", False
    )


def cart_payment_update_locking_enabled() -> bool:
    return runtime.get_bool(
        "payin/feature-flags/enable_payin_cart_payment_update_locking.bool", False
    )
