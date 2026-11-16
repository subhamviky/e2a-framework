"""v1.7.5: egress DLP's card pattern does not match a fragment of a hyphenated or
alphanumeric identifier (the all-digit tail of a UUID), and still masks card numbers."""
import uuid

import pytest

from e2a import DLP_MARKER
from e2a_simulated_llm import SimulatedLLMService


def redact(text):
    return SimulatedLLMService()._redact_sensitive(text)


def test_a_uuid_whose_last_group_is_all_digits_is_not_redacted():
    # a Luhn-valid 16-digit run after the last hyphen: the pattern before v1.7.5 masked it
    text = 'Settlement 5ede915b-5993-44e2-8a31-4111111111111111 is COMPLETED'
    assert redact(text) == (text, 0)


def test_digits_joined_to_a_word_are_not_a_card():
    for text in ('ref ORD4111111111111111', 'id 4111111111111111x', 'key a-4111111111111111'):
        assert redact(text) == (text, 0), text


@pytest.mark.parametrize('card', ['4111 1111 1111 1111', '4111-1111-1111-1111', '4111111111111111',
                                  '378282246310005', '6011 0009 9013 9424'])
def test_card_numbers_are_still_redacted(card):
    for text in (f'card {card} on file', f'{card}', f'card:{card}.', f'({card})'):
        out, n = redact(text)
        assert n == 1 and DLP_MARKER in out and card not in out, text


def test_a_luhn_invalid_number_is_kept_and_random_uuids_never_redact():
    assert redact('order 1234 5678 9012 3456') == ('order 1234 5678 9012 3456', 0)
    for _ in range(2000):
        u = str(uuid.uuid4())
        assert redact(f'id {u} done') == (f'id {u} done', 0)
