import time
import decimal
import sys
import json
import logging
logger = logging.getLogger(__name__)
import apsw
import inspect
import requests
from datetime import datetime
from dateutil.tz import tzlocal
from operator import itemgetter
import fractions
import warnings
import binascii
import re
import hashlib
import sha3
import bitcoin as bitcoinlib
import os
import collections
import threading
import random
import itertools

from counterpartylib.lib import exceptions
from counterpartylib.lib.exceptions import DecodeError
from counterpartylib.lib import config

D = decimal.Decimal
B26_DIGITS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

# subasset contain only characters a-zA-Z0-9.-_@!
SUBASSET_DIGITS = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_@!'
SUBASSET_REVERSE = {'a':1,'b':2,'c':3,'d':4,'e':5,'f':6,'g':7,'h':8,'i':9,'j':10,'k':11,'l':12,'m':13,'n':14,
                    'o':15,'p':16,'q':17,'r':18,'s':19,'t':20,'u':21,'v':22,'w':23,'x':24,'y':25,'z':26,
                    'A':27,'B':28,'C':29,'D':30,'E':31,'F':32,'G':33,'H':34,'I':35,'J':36,'K':37,'L':38,'M':39,
                    'N':40,'O':41,'P':42,'Q':43,'R':44,'S':45,'T':46,'U':47,'V':48,'W':49,'X':50,'Y':51,'Z':52,
                    '0':53,'1':54,'2':55,'3':56,'4':57,'5':58,'6':59,'7':60,'8':61,'9':62,'.':63,'-':64,'_':65,'@':66,'!':67}

# Obsolete in Python 3.4, with enum module.
BET_TYPE_NAME = {0: 'BullCFD', 1: 'BearCFD', 2: 'Equal', 3: 'NotEqual'}
BET_TYPE_ID = {'BullCFD': 0, 'BearCFD': 1, 'Equal': 2, 'NotEqual': 3}

json_dump = lambda x: json.dumps(x, sort_keys=True, indent=4)
json_print = lambda x: print(json_dump(x))

BLOCK_LEDGER = []

CURRENT_BLOCK_INDEX = None

CURR_DIR = os.path.dirname(os.path.realpath(__file__))
with open(CURR_DIR + '/../protocol_changes.json') as f:
    PROTOCOL_CHANGES = json.load(f)

class RPCError (Exception): pass

# TODO: Move to `util_test.py`.
# TODO: This doesn’t timeout properly. (If server hangs, then unhangs, no result.)
def api(method, params):
    """Poll API via JSON-RPC."""
    headers = {'content-type': 'application/json'}
    payload = {
        "method": method,
        "params": params,
        "jsonrpc": "2.0",
        "id": 0,
    }

    response = requests.post(config.RPC, data=json.dumps(payload), headers=headers)
    if response == None:
        raise RPCError('Cannot communicate with {} server.'.format(config.XCP_NAME))
    elif response.status_code != 200:
        if response.status_code == 500:
            raise RPCError('Malformed API call.')
        else:
            raise RPCError(str(response.status_code) + ' ' + response.reason)

    response_json = response.json()
    if 'error' not in response_json.keys() or response_json['error'] == None:
        try:
            return response_json['result']
        except KeyError:
            raise RPCError(response_json)
    else:
        raise RPCError('{} ({})'.format(response_json['error']['message'], response_json['error']['code']))

def chunkify(l, n):
    n = max(1, n)
    return [l[i:i + n] for i in range(0, len(l), n)]

def flat(z):
    return [x for x in z]

def py34TupleAppend(first_elem, t):
    # Had to do it this way to support python 3.4, if we start
    # using the 3.5 runtime this can be replaced by:
    #  (first_elem, *t)

    l = list(t)
    l.insert(0, first_elem)
    return tuple(l)

def accumulate(l):
    it = itertools.groupby(l, itemgetter(0))
    for key, subiter in it:
       yield key, sum(item[1] for item in subiter)

def date_passed(date):
    """Check if the date has already passed."""
    return date <= int(time.time())

def price (numerator, denominator):
    """Return price as Fraction or Decimal."""
    if CURRENT_BLOCK_INDEX >= 294500 or config.TESTNET or config.REGTEST: # Protocol change.
        return fractions.Fraction(numerator, denominator)
    else:
        numerator = D(numerator)
        denominator = D(denominator)
        return D(numerator / denominator)

def last_message(db):
    """Return latest message from the db."""
    cursor = db.cursor()
    messages = list(cursor.execute('''SELECT * FROM messages WHERE message_index = (SELECT MAX(message_index) from messages)'''))
    if messages:
        assert len(messages) == 1
        last_message = messages[0]
    else:
        raise exceptions.DatabaseError('No messages found.')
    cursor.close()
    return last_message

def generate_asset_id(asset_name, block_index):
    """Create asset_id from asset_name."""
    if asset_name == config.BTC: return 0
    elif asset_name == config.XCP: return 1

    if len(asset_name) < 4:
        raise exceptions.AssetNameError('too short')

    # Numeric asset names.
    if enabled('numeric_asset_names'):  # Protocol change.
        if asset_name[0] == 'A':
            # Must be numeric.
            try:
                asset_id = int(asset_name[1:])
            except ValueError:
                raise exceptions.AssetNameError('non‐numeric asset name starts with ‘A’')

            # Number must be in range.
            if not (26**12 + 1 <= asset_id <= 2**64 - 1):
                raise exceptions.AssetNameError('numeric asset name not in range')

            return asset_id
        elif len(asset_name) >= 13:
            raise exceptions.AssetNameError('long asset names must be numeric')

    if asset_name[0] == 'A': raise exceptions.AssetNameError('non‐numeric asset name starts with ‘A’')

    # Convert the Base 26 string to an integer.
    n = 0
    for c in asset_name:
        n *= 26
        if c not in B26_DIGITS:
            raise exceptions.AssetNameError('invalid character:', c)
        digit = B26_DIGITS.index(c)
        n += digit
    asset_id = n

    if asset_id < 26**3:
        raise exceptions.AssetNameError('too short')

    return asset_id

def generate_asset_name (asset_id, block_index):
    """Create asset_name from asset_id."""
    if asset_id == 0: return config.BTC
    elif asset_id == 1: return config.XCP

    if asset_id < 26**3:
        raise exceptions.AssetIDError('too low')

    if enabled('numeric_asset_names'):  # Protocol change.
        if asset_id <= 2**64 - 1:
            if 26**12 + 1 <= asset_id:
                asset_name = 'A' + str(asset_id)
                return asset_name
        else:
            raise exceptions.AssetIDError('too high')

    # Divide that integer into Base 26 string.
    res = []
    n = asset_id
    while n > 0:
        n, r = divmod (n, 26)
        res.append(B26_DIGITS[r])
    asset_name = ''.join(res[::-1])

    """
    return asset_name + checksum.compute(asset_name)
    """
    return asset_name


def get_asset_id (db, asset_name, block_index):
    """Return asset_id from asset_name."""
    if not enabled('hotfix_numeric_assets'):
        return generate_asset_id(asset_name, block_index)
    cursor = db.cursor()
    cursor.execute('''SELECT * FROM assets WHERE asset_name = ?''', (asset_name,))
    assets = list(cursor)
    if len(assets) == 1:
        return int(assets[0]['asset_id'])
    else:
        raise exceptions.AssetError('No such asset: {}'.format(asset_name))

def get_asset_name (db, asset_id, block_index):
    """Return asset_name from asset_id."""
    if not enabled('hotfix_numeric_assets'):
        return generate_asset_name(asset_id, block_index)
    cursor = db.cursor()
    cursor.execute('''SELECT * FROM assets WHERE asset_id = ?''', (str(asset_id),))
    assets = list(cursor)
    if len(assets) == 1:
        return assets[0]['asset_name']
    elif not assets:
        return 0    # Strange, I know…

# If asset_name is an existing subasset (PARENT.child) then return the corresponding numeric asset name (A12345)
#   If asset_name is not an existing subasset, then return the unmodified asset_name
def resolve_subasset_longname(db, asset_name):
    if enabled('subassets'):
        subasset_longname = None
        try:
            subasset_parent, subasset_longname = parse_subasset_from_asset_name(asset_name)
        except Exception as e:
            logger.warn("Invalid subasset {}".format(asset_name))
            subasset_longname = None

        if subasset_longname is not None:
            cursor = db.cursor()
            cursor.execute('''SELECT asset_name FROM assets WHERE asset_longname = ?''', (subasset_longname,))
            assets = list(cursor)
            cursor.close()
            if len(assets) == 1:
                return assets[0]['asset_name']

    return asset_name


# checks and validates subassets (PARENT.SUBASSET)
#   throws exceptions for assset or subasset names with invalid syntax
#   returns (None, None) if the asset is not a subasset name
def parse_subasset_from_asset_name(asset):
    subasset_parent = None
    subasset_child = None
    subasset_longname = None
    chunks = asset.split('.', 1)
    if (len(chunks) == 2):
        subasset_parent = chunks[0]
        subasset_child = chunks[1]
        subasset_longname = asset

        # validate parent asset
        validate_subasset_parent_name(subasset_parent)

        # validate child asset
        validate_subasset_longname(subasset_longname, subasset_child)

    return (subasset_parent, subasset_longname)

# throws exceptions for invalid subasset names
def validate_subasset_longname(subasset_longname, subasset_child=None):
    if subasset_child is None:
        chunks = subasset_longname.split('.', 1)
        if (len(chunks) == 2):
            subasset_child = chunks[1]
        else:
            subasset_child = ''

    if len(subasset_child) < 1:
        raise exceptions.AssetNameError('subasset name too short')
    if len(subasset_longname) > 250:
        raise exceptions.AssetNameError('subasset name too long')

    # can't start with period, can't have consecutive periods, can't contain anything not in SUBASSET_DIGITS
    previous_digit = '.'
    for c in subasset_child:
        if c not in SUBASSET_DIGITS:
            raise exceptions.AssetNameError('subasset name contains invalid character:', c)
        if c == '.' and previous_digit == '.':
            raise exceptions.AssetNameError('subasset name contains consecutive periods')
        previous_digit = c
    if previous_digit == '.':
        raise exceptions.AssetNameError('subasset name ends with a period')

    return True

# throws exceptions for invalid subasset names
def validate_subasset_parent_name(asset_name):
    if asset_name == config.BTC:
        raise exceptions.AssetNameError('parent asset cannot be {}'.format(config.BTC))
    if asset_name == config.XCP:
        raise exceptions.AssetNameError('parent asset cannot be {}'.format(config.XCP))
    if len(asset_name) < 4:
        raise exceptions.AssetNameError('parent asset name too short')
    if len(asset_name) >= 13:
        raise exceptions.AssetNameError('parent asset name too long')
    if asset_name[0] == 'A':
        raise exceptions.AssetNameError('parent asset name starts with ‘A’')
    for c in asset_name:
        if c not in B26_DIGITS:
            raise exceptions.AssetNameError('parent asset name contains invalid character:', c)
    return True

def compact_subasset_longname(string):
    """Compacts a subasset name string into an array of bytes to save space using a base68 encoding scheme.
    Assumes all characters provided belong to SUBASSET_DIGITS.
    """
    name_int = 0
    for i, c in enumerate(string[::-1]):
        name_int += (68 ** i) * SUBASSET_REVERSE[c]
    return name_int.to_bytes((name_int.bit_length() + 7) // 8, byteorder='big')

def expand_subasset_longname(raw_bytes):
    """Expands an array of bytes into a subasset name string."""
    integer = int.from_bytes(raw_bytes, byteorder='big')
    if integer == 0:
        return ''
    ret = ''
    while integer != 0:
        ret = SUBASSET_DIGITS[integer % 68 - 1] + ret
        integer //= 68
    return ret

def generate_random_asset ():
    return 'A' + str(random.randint(26**12 + 1, 2**64 - 1))

def parse_options_from_string(string):
    """Parse options integer from string, if exists."""
    string_list = string.split(" ")
    if len(string_list) == 2:
        try:
            options = int(string_list.pop())
        except:
            raise exceptions.OptionsError('options not an integer')
        return options
    else:
        return False

def validate_address_options(options):
    """Ensure the options are all valid and in range."""
    if (options > config.MAX_INT) or (options < 0):
        raise exceptions.OptionsError('options integer overflow')
    elif options > config.ADDRESS_OPTION_MAX_VALUE:
        raise exceptions.OptionsError('options out of range')
    elif not active_options(config.ADDRESS_OPTION_MAX_VALUE, options):
        raise exceptions.OptionsError('options not possible')

def active_options(config, options):
    """Checks if options active in some given config."""
    return config & options == options

class DebitError (Exception): pass
def debit (db, address, asset, quantity, action=None, event=None):
    """Debit given address by quantity of asset."""
    block_index = CURRENT_BLOCK_INDEX

    if type(quantity) != int:
        raise DebitError('Quantity must be an integer.')
    if quantity < 0:
        raise DebitError('Negative quantity.')
    if quantity > config.MAX_INT:
        raise DebitError('Quantity can\'t be higher than MAX_INT.')
    if asset == config.BTC:
        raise DebitError('Cannot debit bitcoins.')

    debit_cursor = db.cursor()

    # Contracts can only hold XCP balances.
    if enabled('contracts_only_xcp_balances'): # Protocol change.
        if len(address) == 40:
            assert asset == config.XCP

    if asset == config.BTC:
        raise exceptions.BalanceError('Cannot debit bitcoins from a {} address!'.format(config.XCP_NAME))

    debit_cursor.execute('''SELECT * FROM balances \
                            WHERE (address = ? AND asset = ?)''', (address, asset))
    balances = debit_cursor.fetchall()
    if not len(balances) == 1:
        old_balance = 0
    else:
        old_balance = balances[0]['quantity']

    if old_balance < quantity:
        raise DebitError('Insufficient funds.')

    balance = round(old_balance - quantity)
    balance = min(balance, config.MAX_INT)
    assert balance >= 0

    bindings = {
        'quantity': balance,
        'address': address,
        'asset': asset
    }
    sql='update balances set quantity = :quantity where (address = :address and asset = :asset)'
    debit_cursor.execute(sql, bindings)

    # Record debit.
    bindings = {
        'block_index': block_index,
        'address': address,
        'asset': asset,
        'quantity': quantity,
        'action': action,
        'event': event
    }
    sql='insert into debits values(:block_index, :address, :asset, :quantity, :action, :event)'
    debit_cursor.execute(sql, bindings)
    debit_cursor.close()

    BLOCK_LEDGER.append('{}{}{}{}'.format(block_index, address, asset, quantity))

class CreditError (Exception): pass
def credit (db, address, asset, quantity, action=None, event=None):
    """Credit given address by quantity of asset."""
    block_index = CURRENT_BLOCK_INDEX

    if type(quantity) != int:
        raise CreditError('Quantity must be an integer.')
    if quantity < 0:
        raise CreditError('Negative quantity.')
    if quantity > config.MAX_INT:
        raise CreditError('Quantity can\'t be higher than MAX_INT.')
    if asset == config.BTC:
        raise CreditError('Cannot debit bitcoins.')

    credit_cursor = db.cursor()

    # Contracts can only hold XCP balances.
    if enabled('contracts_only_xcp_balances'): # Protocol change.
        if len(address) == 40:
            assert asset == config.XCP

    credit_cursor.execute('''SELECT * FROM balances \
                             WHERE (address = ? AND asset = ?)''', (address, asset))
    balances = credit_cursor.fetchall()
    if len(balances) == 0:
        assert balances == []

        #update balances table with new balance
        bindings = {
            'address': address,
            'asset': asset,
            'quantity': quantity,
        }
        sql='insert into balances values(:address, :asset, :quantity)'
        credit_cursor.execute(sql, bindings)
    elif len(balances) > 1:
        assert False
    else:
        old_balance = balances[0]['quantity']
        assert type(old_balance) == int
        balance = round(old_balance + quantity)
        balance = min(balance, config.MAX_INT)

        bindings = {
            'quantity': balance,
            'address': address,
            'asset': asset
        }
        sql='update balances set quantity = :quantity where (address = :address and asset = :asset)'
        credit_cursor.execute(sql, bindings)

    # Record credit.
    bindings = {
        'block_index': block_index,
        'address': address,
        'asset': asset,
        'quantity': quantity,
        'action': action,
        'event': event
    }
    sql='insert into credits values(:block_index, :address, :asset, :quantity, :action, :event)'

    credit_cursor.execute(sql, bindings)
    credit_cursor.close()

    BLOCK_LEDGER.append('{}{}{}{}'.format(block_index, address, asset, quantity))

class QuantityError(Exception): pass

def is_divisible(db, asset):
    """Check if the asset is divisible."""
    if asset in (config.BTC, config.XCP):
        return True
    else:
        cursor = db.cursor()
        cursor.execute('''SELECT * FROM issuances \
                          WHERE (status = ? AND asset = ?) ORDER BY tx_index DESC''', ('valid', asset))
        issuances = cursor.fetchall()
        if not issuances: raise exceptions.AssetError('No such asset: {}'.format(asset))
        return issuances[0]['divisible']

def value_input(quantity, asset, divisible):
    if asset == 'leverage':
        return round(quantity)

    if asset in ('value', 'fraction', 'price', 'odds'):
        return float(quantity)  # TODO: Float?!

    if divisible:
        quantity = D(quantity) * config.UNIT
        if quantity == quantity.to_integral():
            return int(quantity)
        else:
            raise QuantityError('Divisible assets have only eight decimal places of precision.')
    else:
        quantity = D(quantity)
        if quantity != round(quantity):
            raise QuantityError('Fractional quantities of indivisible assets.')
        return round(quantity)

def value_in(db, quantity, asset, divisible=None):
    if asset not in ['leverage', 'value', 'fraction', 'price', 'odds'] and divisible == None:
        divisible = is_divisible(db, asset)
    return value_input(quantity, asset, divisible)

def value_output(quantity, asset, divisible):

    def norm(num, places):
        """Round only if necessary."""
        num = round(num, places)
        fmt = '{:.' + str(places) + 'f}'
        num = fmt.format(num)
        return num.rstrip('0')+'0' if num.rstrip('0')[-1] == '.' else num.rstrip('0')

    if asset == 'fraction':
        return str(norm(D(quantity) * D(100), 6)) + '%'

    if asset in ('leverage', 'value', 'price', 'odds'):
        return norm(quantity, 6)

    if divisible:
        quantity = D(quantity) / D(config.UNIT)
        if quantity == quantity.to_integral():
            return str(quantity) + '.0'  # For divisible assets, display the decimal point.
        else:
            return norm(quantity, 8)
    else:
        quantity = D(quantity)
        if quantity != round(quantity):
            raise QuantityError('Fractional quantities of indivisible assets.')
        return round(quantity)

def value_out(db, quantity, asset, divisible=None):
    if asset not in ['leverage', 'value', 'fraction', 'price', 'odds'] and divisible == None:
        divisible = is_divisible(db, asset)
    return value_output(quantity, asset, divisible)

### SUPPLIES ###

def holders(db, asset, exclude_empty_holders=False):
    """Return holders of the asset."""
    holders = []
    cursor = db.cursor()
    # Balances
    if exclude_empty_holders:
        cursor.execute('''SELECT * FROM balances \
                          WHERE asset = ? AND quantity > ?''', (asset, 0))
    else:
        cursor.execute('''SELECT * FROM balances \
                          WHERE asset = ?''', (asset, ))

    for balance in list(cursor):
        holders.append({'address': balance['address'], 'address_quantity': balance['quantity'], 'escrow': None})
    # Funds escrowed in orders. (Protocol change.)
    cursor.execute('''SELECT * FROM orders \
                      WHERE give_asset = ? AND status = ?''', (asset, 'open'))
    for order in list(cursor):
        holders.append({'address': order['source'], 'address_quantity': order['give_remaining'], 'escrow': order['tx_hash']})
    # Funds escrowed in pending order matches. (Protocol change.)
    cursor.execute('''SELECT * FROM order_matches \
                      WHERE (forward_asset = ? AND status = ?)''', (asset, 'pending'))
    for order_match in list(cursor):
        holders.append({'address': order_match['tx0_address'], 'address_quantity': order_match['forward_quantity'], 'escrow': order_match['id']})
    cursor.execute('''SELECT * FROM order_matches \
                      WHERE (backward_asset = ? AND status = ?)''', (asset, 'pending'))
    for order_match in list(cursor):
        holders.append({'address': order_match['tx1_address'], 'address_quantity': order_match['backward_quantity'], 'escrow': order_match['id']})

    # Bets and RPS (and bet/rps matches) only escrow XCP.
    if asset == config.XCP:
        cursor.execute('''SELECT * FROM bets \
                          WHERE status = ?''', ('open',))
        for bet in list(cursor):
            holders.append({'address': bet['source'], 'address_quantity': bet['wager_remaining'], 'escrow': bet['tx_hash']})
        cursor.execute('''SELECT * FROM bet_matches \
                          WHERE status = ?''', ('pending',))
        for bet_match in list(cursor):
            holders.append({'address': bet_match['tx0_address'], 'address_quantity': bet_match['forward_quantity'], 'escrow': bet_match['id']})
            holders.append({'address': bet_match['tx1_address'], 'address_quantity': bet_match['backward_quantity'], 'escrow': bet_match['id']})

        cursor.execute('''SELECT * FROM rps \
                          WHERE status = ?''', ('open',))
        for rps in list(cursor):
            holders.append({'address': rps['source'], 'address_quantity': rps['wager'], 'escrow': rps['tx_hash']})
        cursor.execute('''SELECT * FROM rps_matches \
                          WHERE status IN (?, ?, ?)''', ('pending', 'pending and resolved', 'resolved and pending'))
        for rps_match in list(cursor):
            holders.append({'address': rps_match['tx0_address'], 'address_quantity': rps_match['wager'], 'escrow': rps_match['id']})
            holders.append({'address': rps_match['tx1_address'], 'address_quantity': rps_match['wager'], 'escrow': rps_match['id']})

    if enabled('dispensers_in_holders'):
        # Funds escrowed in dispensers.
        cursor.execute('''SELECT * FROM dispensers \
                          WHERE asset = ? AND status = ?''', (asset, 0))


        for dispenser in list(cursor):
            holders.append({'address': dispenser['source'], 'address_quantity': dispenser['give_remaining'], 'escrow': None})

    cursor.close()
    return holders

def xcp_created (db):
    """Return number of XCP created thus far."""
    cursor = db.cursor()
    cursor.execute('''SELECT SUM(earned) AS total FROM burns \
                      WHERE (status = ?)''', ('valid',))
    total = list(cursor)[0]['total'] or 0
    cursor.close()
    return total

def xcp_destroyed (db):
    """Return number of XCP destroyed thus far."""
    cursor = db.cursor()
    # Destructions
    cursor.execute('''SELECT SUM(quantity) AS total FROM destructions \
                      WHERE (status = ? AND asset = ?)''', ('valid', config.XCP))
    destroyed_total = list(cursor)[0]['total'] or 0
    # Subtract issuance fees.
    cursor.execute('''SELECT SUM(fee_paid) AS total FROM issuances\
                      WHERE status = ?''', ('valid',))
    issuance_fee_total = list(cursor)[0]['total'] or 0
    # Subtract dividend fees.
    cursor.execute('''SELECT SUM(fee_paid) AS total FROM dividends\
                      WHERE status = ?''', ('valid',))
    dividend_fee_total = list(cursor)[0]['total'] or 0
    # Subtract sweep fees.
    cursor.execute('''SELECT SUM(fee_paid) AS total FROM sweeps\
                      WHERE status = ?''', ('valid',))
    sweeps_fee_total = list(cursor)[0]['total'] or 0
    cursor.close()
    return destroyed_total + issuance_fee_total + dividend_fee_total + sweeps_fee_total

def xcp_supply (db):
    """Return the XCP supply."""
    return xcp_created(db) - xcp_destroyed(db)

def creations (db):
    """Return creations."""
    cursor = db.cursor()
    creations = {config.XCP: xcp_created(db)}
    cursor.execute('''SELECT asset, SUM(quantity) AS created FROM issuances \
                      WHERE status = ? GROUP BY asset''', ('valid',))

    for issuance in cursor:
        asset = issuance['asset']
        created = issuance['created']
        creations[asset] = created

    cursor.close()
    return creations

def destructions (db):
    """Return destructions."""
    cursor = db.cursor()
    destructions = {config.XCP: xcp_destroyed(db)}
    cursor.execute('''SELECT asset, SUM(quantity) AS destroyed FROM destructions \
                      WHERE (status = ? AND asset != ?) GROUP BY asset''', ('valid', config.XCP))

    for destruction in cursor:
        asset = destruction['asset']
        destroyed = destruction['destroyed']
        destructions[asset] = destroyed

    cursor.close()
    return destructions

def asset_issued_total (db, asset):
    """Return asset total issued."""
    cursor = db.cursor()
    cursor.execute('''SELECT SUM(quantity) AS total FROM issuances \
                      WHERE (status = ? AND asset = ?)''', ('valid', asset))
    issued_total = list(cursor)[0]['total'] or 0
    cursor.close()
    return issued_total

def asset_destroyed_total (db, asset):
    """Return asset total destroyed."""
    cursor = db.cursor()
    cursor.execute('''SELECT SUM(quantity) AS total FROM destructions \
                      WHERE (status = ? AND asset = ?)''', ('valid', asset))
    destroyed_total = list(cursor)[0]['total'] or 0
    cursor.close()
    return destroyed_total

def asset_supply (db, asset):
    """Return asset supply."""
    return asset_issued_total(db, asset) - asset_destroyed_total(db, asset)

def supplies (db):
    """Return supplies."""
    d1 = creations(db)
    d2 = destructions(db)
    return {key: d1[key] - d2.get(key, 0) for key in d1.keys()}

def held (db): #TODO: Rename ?
    queries = [
        "SELECT asset, SUM(quantity) AS total FROM balances GROUP BY asset",
        "SELECT give_asset AS asset, SUM(give_remaining) AS total FROM orders WHERE status = 'open' GROUP BY asset",
        "SELECT give_asset AS asset, SUM(give_remaining) AS total FROM orders WHERE status = 'filled' and give_asset = 'XCP' and get_asset = 'BTC' GROUP BY asset",
        "SELECT forward_asset AS asset, SUM(forward_quantity) AS total FROM order_matches WHERE status = 'pending' GROUP BY asset",
        "SELECT backward_asset AS asset, SUM(backward_quantity) AS total FROM order_matches WHERE status = 'pending' GROUP BY asset",
        "SELECT 'XCP' AS asset, SUM(wager_remaining) AS total FROM bets WHERE status = 'open'",
        "SELECT 'XCP' AS asset, SUM(forward_quantity) AS total FROM bet_matches WHERE status = 'pending'",
        "SELECT 'XCP' AS asset, SUM(backward_quantity) AS total FROM bet_matches WHERE status = 'pending'",
        "SELECT 'XCP' AS asset, SUM(wager) AS total FROM rps WHERE status = 'open'",
        "SELECT 'XCP' AS asset, SUM(wager * 2) AS total FROM rps_matches WHERE status IN ('pending', 'pending and resolved', 'resolved and pending')",
        "SELECT asset, SUM(give_remaining) AS total FROM dispensers WHERE status=0 OR status=1 GROUP BY asset",
    ]

    sql = "SELECT asset, SUM(total) AS total FROM (" + " UNION ALL ".join(queries) + ") GROUP BY asset;"

    cursor = db.cursor()
    cursor.execute(sql)
    held = {}
    for row in cursor:
        asset = row['asset']
        total = row['total']
        held[asset] = total

    return held

### SUPPLIES ###


class GetURLError (Exception): pass
def get_url(url, abort_on_error=False, is_json=True, fetch_timeout=5):
    """Fetch URL using requests.get."""
    try:
        r = requests.get(url, timeout=fetch_timeout)
    except Exception as e:
        raise GetURLError("Got get_url request error: %s" % e)
    else:
        if r.status_code != 200 and abort_on_error:
            raise GetURLError("Bad status code returned: '%s'. result body: '%s'." % (r.status_code, r.text))
        result = json.loads(r.text) if is_json else r.text
    return result


def dhash(text):
    if not isinstance(text, bytes):
        text = bytes(str(text), 'utf-8')

    return hashlib.sha256(hashlib.sha256(text).digest()).digest()


def dhash_string(text):
    return binascii.hexlify(dhash(text)).decode()


def get_balance (db, address, asset):
    """Get balance of contract or address."""
    cursor = db.cursor()
    balances = list(cursor.execute('''SELECT * FROM balances WHERE (address = ? AND asset = ?)''', (address, asset)))
    cursor.close()
    if not balances: return 0
    else: return balances[0]['quantity']

# Why on Earth does `binascii.hexlify()` return bytes?!
def hexlify(x):
    """Return the hexadecimal representation of the binary data. Decode from ASCII to UTF-8."""
    return binascii.hexlify(x).decode('ascii')
def unhexlify(hex_string):
    return binascii.unhexlify(bytes(hex_string, 'utf-8'))

### Protocol Changes ###
def enabled(change_name, block_index=None):
    """Return True if protocol change is enabled."""
    if config.REGTEST:
        return True # All changes are always enabled on REGTEST

    if config.TESTNET:
        index_name = 'testnet_block_index'
    else:
        index_name = 'block_index'

    enable_block_index = PROTOCOL_CHANGES[change_name][index_name]

    if not block_index:
        block_index = CURRENT_BLOCK_INDEX

    if block_index >= enable_block_index:
        return True
    else:
        return False

def get_value_by_block_index(change_name, block_index=None):

    if not block_index:
        block_index = CURRENT_BLOCK_INDEX
    
    if config.REGTEST:
        max_block_index_testnet = -1
        for key, value in PROTOCOL_CHANGES[change_name]["testnet"]:
            if int(key) > int(max_block_index):
                max_block_index = key
            
        return PROTOCOL_CHANGES[change_name]["testnet"][max_block_index]["value"]
    
    if config.TESTNET:
        index_name = 'testnet'        
    else:
        index_name = 'mainnet'

    max_block_index = -1
    for key in PROTOCOL_CHANGES[change_name][index_name]:
        if int(key) > int(max_block_index) and block_index >= int(key):
            max_block_index = key
            
    return PROTOCOL_CHANGES[change_name][index_name][max_block_index]["value"]

def transfer(db, source, destination, asset, quantity, action, event):
    """Transfer quantity of asset from source to destination."""
    debit(db, source, asset, quantity, action=action, event=event)
    credit(db, destination, asset, quantity, action=action, event=event)

ID_SEPARATOR = '_'
def make_id(hash_1, hash_2):
    return hash_1 + ID_SEPARATOR + hash_2
def parse_id(match_id):
    assert match_id[64] == ID_SEPARATOR
    return match_id[:64], match_id[65:] # UTF-8 encoding means that the indices are doubled.

def sizeof(v):
    if isinstance(v, dict) or isinstance(v, DictCache):
        s = 0
        for dk, dv in v.items():
            s += sizeof(dk)
            s += sizeof(dv)

        return s
    else:
        return sys.getsizeof(v)

class DictCache:
    """Threadsafe FIFO dict cache"""
    def __init__(self, size=100):
        if int(size) < 1 :
            raise AttributeError('size < 1 or not a number')
        self.size = size
        self.dict = collections.OrderedDict()
        self.lock = threading.Lock()

    def __getitem__(self,key):
        with self.lock:
            return self.dict[key]

    def __setitem__(self,key,value):
        with self.lock:
            while len(self.dict) >= self.size:
                self.dict.popitem(last=False)
            self.dict[key] = value

    def __delitem__(self,key):
        with self.lock:
            del self.dict[key]

    def __len__(self):
        with self.lock:
            return len(self.dict)

    def __contains__(self, key):
        with self.lock:
            return key in self.dict

    def refresh(self, key):
        with self.lock:
            self.dict.move_to_end(key, last=True)


URL_USERNAMEPASS_REGEX = re.compile('.+://(.+)@')
def clean_url_for_log(url):
    m = URL_USERNAMEPASS_REGEX.match(url)
    if m and m.group(1):
        url = url.replace(m.group(1), 'XXXXXXXX')

    return url

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

# ORACLES
def satoshirate_to_fiat(satoshirate):
    return round(satoshirate/100.0,2)

def get_oracle_last_price(db, oracle_address, block_index):
    cursor = db.cursor()
    cursor.execute('SELECT * FROM broadcasts WHERE source=:source AND status=:status AND block_index<:block_index ORDER by tx_index DESC LIMIT 1', {
        'source': oracle_address,
        'status': 'valid',
        'block_index': block_index
    })
    broadcasts = cursor.fetchall()
    cursor.close()
    
    if len(broadcasts) == 0:
        return None, None
    
    oracle_broadcast = broadcasts[0]
    oracle_label = oracle_broadcast["text"].split("-")
    if len(oracle_label) == 2:
        fiat_label = oracle_label[1]
    else:   
        fiat_label = ""
    
    return oracle_broadcast['value'], oracle_broadcast['fee_fraction_int'], fiat_label, oracle_broadcast['block_index']