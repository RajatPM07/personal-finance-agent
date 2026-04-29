def test_money_sent_to_with_own_handle_is_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Money sent to Rajat Sharma",
        other_transaction_details="UPI Ref ABC / 7358467199@ptsbi",
        own_handles=own,
    ) is True


def test_money_sent_to_other_handle_is_not_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Money sent to Md Mehboob",
        other_transaction_details="9123456789@upi",
        own_handles=own,
    ) is False


def test_paid_to_prefix_is_never_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    # Even if the row has the own-handle in the UPI ID column (theoretical),
    # 'Paid to ...' is a merchant payment, not a self-transfer.
    assert classify_self_transfer(
        transaction_details="Paid to Munni Sharma",
        other_transaction_details="7358467199@ptsbi",
        own_handles=own,
    ) is False


def test_received_from_is_never_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Received from Aayushi Shukla",
        other_transaction_details="7358467199@ptsbi",
        own_handles=own,
    ) is False


def test_empty_own_handles_returns_false():
    """If we have no own-handles configured, no row is a self-transfer.
    (V1 fallback when the accounts table has no UPI-typed rows.)"""
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    assert classify_self_transfer(
        transaction_details="Money sent to Anyone",
        other_transaction_details="some@upi",
        own_handles=[],
    ) is False


def test_none_inputs_handled():
    """NaN/None values from pandas show up as None in some rows; classifier
    must tolerate them without crashing."""
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details=None,
        other_transaction_details=None,
        own_handles=own,
    ) is False
