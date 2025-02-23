import os
import pytest

from test_utils.address import Address
from test_utils.keys import KeyPair
from test_utils.policy import Policy, new_policy_for
from test_utils.vending_machine import vm_test_config

from test_utils.blockfrost import blockfrost_api, get_mainnet_env, get_network_magic, get_preview_env
from test_utils.config import get_funder_address
from test_utils.chain import await_payment, burn_and_reclaim_tada, cardano_cli, find_min_utxos_for_txn, lovelace_in, mint_assets, policy_is_empty, send_money
from test_utils.fs import data_file_path, protocol_file_path
from test_utils.metadata import asset_filename, asset_name_hex, create_asset_files, hex_to_asset_name, metadata_json
from test_utils.process import launch_py3_subprocess

from cardano.wt.mint import Mint
from cardano.wt.nft_vending_machine import NftVendingMachine
from cardano.wt.whitelist.asset_whitelist import SingleUseWhitelist, UnlimitedWhitelist
from cardano.wt.whitelist.no_whitelist import NoWhitelist

DONATION_AMT = 0
EXPIRATION = 87654321
MINT_PRICE = 10000000
SINGLE_VEND_MAX = 10
VEND_RANDOMLY = True
WL_EXPIRATION = 76543210

WL_REBATE = 5000000
PADDING = 500000
MIN_UTXO_PAYMENT = 2000000

def initialize_asset_wl(whitelist_dir, consumed_dir, wl_policy, request, blockfrost_api):
    wl_initializer_args = [
        '--blockfrost-project', blockfrost_api.project,
        '--consumed-dir', consumed_dir,
        '--whitelist-dir', whitelist_dir,
        '--policy-id', wl_policy.id
    ]
    if get_preview_env():
        wl_initializer_args.append('--preview')
    if get_mainnet_env():
        wl_initializer_args.append('--mainnet')
    launch_py3_subprocess(os.path.join('scripts', 'initialize_asset_wl.py'), request, wl_initializer_args).wait()

def test_validate_requires_whitelist_dir_created(request, vm_test_config):
    whitelist = SingleUseWhitelist(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    simple_script = data_file_path(request, os.path.join('scripts', 'simple.script'))
    mint = Mint(None, MINT_PRICE, DONATION_AMT, vm_test_config.metadata_dir, simple_script, None, whitelist)
    try:
        mint.validate()
        assert False, "Successfully validated mint without a whitelist directory"
    except ValueError as e:
        assert f"Could not find whitelist directory {vm_test_config.whitelist_dir}" in str(e)

def test_validate_requires_consumed_dir_created(request, vm_test_config):
    os.mkdir(vm_test_config.whitelist_dir)
    whitelist = SingleUseWhitelist(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    simple_script = data_file_path(request, os.path.join('scripts', 'simple.script'))
    mint = Mint(None, MINT_PRICE, DONATION_AMT, vm_test_config.metadata_dir, simple_script, None, whitelist)
    try:
        mint.validate()
        assert False, "Successfully validated mint without a whitelist directory"
    except ValueError as e:
        assert f"{vm_test_config.consumed_dir} does not exist" in str(e)

def test_no_whitelist_always_whitelists():
    assert NoWhitelist().is_whitelisted('foobar'), "NoWhitelist should always be whitelisted"

@pytest.mark.parametrize("WhitelistType", [SingleUseWhitelist, UnlimitedWhitelist])
def test_rejects_if_no_asset_sent_to_self(request, vm_test_config, blockfrost_api, cardano_cli, WhitelistType):
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_inputs = find_min_utxos_for_txn(WL_REBATE, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            WL_REBATE,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_pass = "WildTangz WL 1"
    wl_pass_onchain = f"{wl_policy.id}{asset_name_hex(wl_pass)}"
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets([wl_pass], wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = WhitelistType(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_amt = MINT_PRICE + PADDING
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    buyer_utxo = await_payment(buyer.address, funding_request_txn, blockfrost_api)

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    mint_payment = lovelace_in(buyer_utxo)
    payment_txn = send_money(
            [payment],
            mint_payment,
            buyer,
            [buyer_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            SINGLE_VEND_MAX,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    asset_name = "WildTangz 1"
    create_asset_files([asset_name], policy, request, vm_test_config.metadata_dir)

    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )

    try:
        await_payment(profit.address, None, blockfrost_api)
        assert False, f"{profit.address} was paid, but should not have been"
    except:
        pass

    created_assets = blockfrost_api.get_assets(policy.id)
    assert not created_assets, f"Somehow the test created assets under {policy.id}: {created_assets}"

    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should have remained on the whitelist"

    burn_payment = lovelace_in(wl_mint_utxo)
    burn_txn = burn_and_reclaim_tada(
            [wl_pass],
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            burn_payment,
            buyer,
            [wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, burn_txn, blockfrost_api)

    minter_utxo = await_payment(buyer.address, None, blockfrost_api)
    drain_payment = lovelace_in(minter_utxo)
    drain_txn = send_money(
            [funder],
            drain_payment,
            buyer,
            [minter_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, drain_txn, blockfrost_api)

@pytest.mark.parametrize("WhitelistType", [SingleUseWhitelist, UnlimitedWhitelist])
def test_remains_on_whitelist_if_vendingmachine_empty(request, vm_test_config, blockfrost_api, cardano_cli, WhitelistType):
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_inputs = find_min_utxos_for_txn(WL_REBATE, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            WL_REBATE,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_pass = "WildTangz WL 1"
    wl_pass_onchain = f"{wl_policy.id}{asset_name_hex(wl_pass)}"
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets([wl_pass], wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = WhitelistType(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_amt = MINT_PRICE + PADDING
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    buyer_utxo = await_payment(buyer.address, funding_request_txn, blockfrost_api)

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    mint_payment = lovelace_in(buyer_utxo)
    payment_txn = send_money(
            [payment],
            mint_payment,
            buyer,
            [buyer_utxo, wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            SINGLE_VEND_MAX,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )

    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should have remained on the whitelist"

    try:
        await_payment(profit.address, None, blockfrost_api)
        assert False, f"{profit.address} was paid, but should not have been"
    except:
        pass

    created_assets = blockfrost_api.get_assets(policy.id)
    assert not created_assets, f"Somehow the test created assets under {policy.id}: {created_assets}"

    all_utxos = blockfrost_api.get_utxos(buyer.address, [])
    assert len(all_utxos) == 2, f"Buyer {buyer.address} did not receive a refund: {all_utxos}"

    burn_payment = sum([lovelace_in(utxo) for utxo in all_utxos])
    burn_txn = burn_and_reclaim_tada(
            [wl_pass],
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            burn_payment,
            buyer,
            all_utxos,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, burn_txn, blockfrost_api)

@pytest.mark.parametrize("WhitelistType", [SingleUseWhitelist, UnlimitedWhitelist])
def test_rejects_if_asset_sent_as_reference_input(request, vm_test_config, blockfrost_api, cardano_cli, WhitelistType):
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_inputs = find_min_utxos_for_txn(WL_REBATE, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            WL_REBATE,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_pass = "WildTangz WL 1"
    wl_pass_onchain = f"{wl_policy.id}{asset_name_hex(wl_pass)}"
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets([wl_pass], wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = WhitelistType(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_amt = MINT_PRICE + PADDING
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    buyer_utxo = await_payment(buyer.address, funding_request_txn, blockfrost_api)

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    mint_payment = lovelace_in(buyer_utxo)
    payment_txn = send_money(
            [payment],
            mint_payment,
            buyer,
            [buyer_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir,
            ref_inputs=[wl_mint_utxo]
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            SINGLE_VEND_MAX,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    asset_name = "WildTangz 1"
    create_asset_files([asset_name], policy, request, vm_test_config.metadata_dir)

    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )

    try:
        await_payment(profit.address, None, blockfrost_api)
        assert False, f"{profit.address} was paid, but should not have been"
    except:
        pass

    created_assets = blockfrost_api.get_assets(policy.id)
    assert not created_assets, f"Somehow the test created assets under {policy.id}: {created_assets}"

    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should have remained on the whitelist"

    burn_payment = lovelace_in(wl_mint_utxo)
    burn_txn = burn_and_reclaim_tada(
            [wl_pass],
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            burn_payment,
            buyer,
            [wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, burn_txn, blockfrost_api)

    minter_utxo = await_payment(buyer.address, None, blockfrost_api)
    drain_payment = lovelace_in(minter_utxo)
    drain_txn = send_money(
            [funder],
            drain_payment,
            buyer,
            [minter_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, drain_txn, blockfrost_api)

def test_excludes_if_asset_sent_directly(request, vm_test_config, blockfrost_api, cardano_cli):
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_amt = MINT_PRICE + PADDING
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_pass = "WildTangz WL 1"
    wl_pass_onchain = f"{wl_policy.id}{asset_name_hex(wl_pass)}"
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets([wl_pass], wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = SingleUseWhitelist(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    mint_payment = lovelace_in(wl_mint_utxo)
    assert mint_payment > MINT_PRICE, f"Test setup failed needed at least {MINT_PRICE} in {wl_buyer_utxo}"
    payment_txn = send_money(
            [payment],
            mint_payment,
            buyer,
            [wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir,
            additional_outputs=f"1 {wl_policy.id}.{asset_name_hex(wl_pass)}"
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            SINGLE_VEND_MAX,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    asset_name = "WildTangz 1"
    create_asset_files([asset_name], policy, request, vm_test_config.metadata_dir)

    exclusions = set()
    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            exclusions
    )
    assert payment_utxo in exclusions, f"Expected {payment_utxo} in exclusions {exclusions}"

    try:
        await_payment(profit.address, None, blockfrost_api)
        assert False, f"{profit.address} was paid, but should not have been"
    except:
        pass

    created_assets = blockfrost_api.get_assets(policy.id)
    assert not created_assets, f"Somehow the test created assets under {policy.id}: {created_assets}"

    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should have remained on the whitelist"

    burn_payment = lovelace_in(payment_utxo)
    burn_txn = burn_and_reclaim_tada(
            [wl_pass],
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            burn_payment,
            payment,
            [payment_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, burn_txn, blockfrost_api)

def test_mints_correct_number_for_single_use(request, vm_test_config, blockfrost_api, cardano_cli):
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_inputs = find_min_utxos_for_txn(WL_REBATE, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            WL_REBATE,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_pass = "WildTangz WL 1"
    wl_pass_onchain = f"{wl_policy.id}{asset_name_hex(wl_pass)}"
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets([wl_pass], wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = SingleUseWhitelist(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_amt = (MINT_PRICE * 2) + PADDING
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    buyer_utxo = await_payment(buyer.address, funding_request_txn, blockfrost_api)

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    mint_payment = lovelace_in(buyer_utxo)
    payment_txn = send_money(
            [payment],
            mint_payment,
            buyer,
            [buyer_utxo, wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            SINGLE_VEND_MAX,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    asset_names = ["WildTangz 1", "WildTangz 2"]
    create_asset_files(asset_names, policy, request, vm_test_config.metadata_dir)

    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )

    assert not whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should NO LONGER be on the whitelist"

    profit_utxo = await_payment(profit.address, None, blockfrost_api)
    profit_txn = blockfrost_api.get_txn(profit_utxo.hash)
    profit_expected = MINT_PRICE - Mint.RebateCalculator.calculate_rebate_for(1, 1, len(asset_names[0])) - int(profit_txn['fees'])
    profit_actual = lovelace_in(profit_utxo)
    assert profit_actual == profit_expected, f"Expected {profit_expected}, but actual was {profit_actual}"

    minted_utxo = await_payment(buyer.address, profit_utxo.hash, blockfrost_api)
    created_assets = blockfrost_api.get_assets(policy.id)
    assert len(created_assets) == 1, f"Test did not create 1 asset under {policy.id}: {created_assets}"
    assert lovelace_in(minted_utxo) > MINT_PRICE, f"Buyer requested two and should have received refund of {MINT_PRICE}"

    minted_assetid = created_assets[0]['asset']
    asset_name = hex_to_asset_name(minted_assetid[56:])
    assert lovelace_in(minted_utxo, policy=policy, asset_name=asset_name) == 1, f"Buyer does not have {asset_name} in {minted_utxo}"
    assert minted_assetid.startswith(policy.id), f"Minted asset {minted_assetid} does not belong to policy {policy.id}"
    assert asset_name in asset_names, f"Minted asset {minted_assetid} does not have hex name {asset_name}"

    minted_asset = blockfrost_api.get_asset(minted_assetid)
    assert minted_asset, f"Could not retrieve {minted_assetid} from the blockchain"
    expected_metadata = metadata_json(request, asset_filename(asset_name))[asset_name]
    assert minted_asset['onchain_metadata'] == expected_metadata, f"Mismatch in metadata: {minted_asset}"

    mint_sendself_utxo = await_payment(buyer.address, payment_txn, blockfrost_api)
    second_payment_txn = send_money(
            [payment],
            MINT_PRICE + PADDING,
            buyer,
            [minted_utxo, mint_sendself_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    second_payment_utxo = await_payment(buyer.address, second_payment_txn, blockfrost_api)

    assert not whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should NO LONGER be on the whitelist"
    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )
    assert not whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should NO LONGER be on the whitelist"

    created_assets = blockfrost_api.get_assets(policy.id)
    assert len(created_assets) == 1 and int(created_assets[0]['quantity']) == 1, f"Test should NOT create second asset under {policy.id}: {created_assets}"

    drain_payment = lovelace_in(profit_utxo)
    drain_txn = send_money(
            [funder],
            drain_payment,
            profit,
            [profit_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, drain_txn, blockfrost_api)

    burn_payment = lovelace_in(second_payment_utxo) - MIN_UTXO_PAYMENT
    burn_txn = burn_and_reclaim_tada(
            [asset_name],
            policy,
            policy_keys,
            EXPIRATION,
            funder,
            burn_payment,
            buyer,
            [second_payment_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    burn_utxo = await_payment(buyer.address, burn_txn, blockfrost_api)

    assert policy_is_empty(policy, blockfrost_api), f"Burned asset successfully but {policy.id} has remaining_assets"

    wl_burn_payment = lovelace_in(burn_utxo)
    wl_burn_txn = burn_and_reclaim_tada(
            [wl_pass],
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            wl_burn_payment,
            buyer,
            [burn_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, wl_burn_txn, blockfrost_api)

    refund_utxo = await_payment(buyer.address, None, blockfrost_api)
    refund_payment = lovelace_in(refund_utxo)
    assert refund_payment > MINT_PRICE, f"Expecting refund greater than {MINT_PRICE} instead found {refund_utxo}"
    refund_txn = send_money(
            [funder],
            refund_payment,
            buyer,
            [refund_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, refund_txn, blockfrost_api)

def test_mints_correct_number_for_multiple_passes(request, vm_test_config, blockfrost_api, cardano_cli):
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_inputs = find_min_utxos_for_txn(WL_REBATE, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            WL_REBATE,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_passes = ["WildTangz WL 1", "WildTangz WL 2"]
    wl_passes_onchain = [f"{wl_policy.id}{asset_name_hex(wl_pass)}" for wl_pass in wl_passes]
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets(wl_passes, wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = SingleUseWhitelist(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    for wl_pass_onchain in wl_passes_onchain:
        assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_amt = (MINT_PRICE * 3) + PADDING
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    buyer_utxo = await_payment(buyer.address, funding_request_txn, blockfrost_api)

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    mint_payment = lovelace_in(buyer_utxo)
    payment_txn = send_money(
            [payment],
            mint_payment,
            buyer,
            [buyer_utxo, wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            SINGLE_VEND_MAX,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    asset_names = ["WildTangz 1", "WildTangz 2", "WildTangz 3"]
    create_asset_files(asset_names, policy, request, vm_test_config.metadata_dir)

    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )

    for wl_pass_onchain in wl_passes_onchain:
        assert not whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should NO LONGER be on the whitelist"

    profit_utxo = await_payment(profit.address, None, blockfrost_api)
    profit_txn = blockfrost_api.get_txn(profit_utxo.hash)
    profit_expected = (MINT_PRICE * 2) - Mint.RebateCalculator.calculate_rebate_for(1, 2, 2 * len(asset_names[0])) - int(profit_txn['fees'])
    profit_actual = lovelace_in(profit_utxo)
    assert profit_actual == profit_expected, f"Expected {profit_expected}, but actual was {profit_actual}"

    minted_utxo = await_payment(buyer.address, profit_utxo.hash, blockfrost_api)
    created_assets = blockfrost_api.get_assets(policy.id)
    assert len(created_assets) == 2, f"Test did not create 1 asset under {policy.id}: {created_assets}"
    for created_asset in created_assets:
        minted_assetid = created_asset['asset']
        asset_name = hex_to_asset_name(minted_assetid[56:])
        assert lovelace_in(minted_utxo, policy=policy, asset_name=asset_name) == 1, f"Buyer does not have {asset_name} in {minted_utxo}"
        assert minted_assetid.startswith(policy.id), f"Minted asset {minted_assetid} does not belong to policy {policy.id}"
        assert asset_name in asset_names, f"Minted asset {minted_assetid} does not have hex name {asset_name}"

        minted_asset = blockfrost_api.get_asset(minted_assetid)
        assert minted_asset, f"Could not retrieve {minted_assetid} from the blockchain"
        expected_metadata = metadata_json(request, asset_filename(asset_name))[asset_name]
        assert minted_asset['onchain_metadata'] == expected_metadata, f"Mismatch in metadata: {minted_asset}"

    mint_sendself_utxo = await_payment(buyer.address, payment_txn, blockfrost_api)
    second_payment_txn = send_money(
            [payment],
            MINT_PRICE + PADDING,
            buyer,
            [minted_utxo, mint_sendself_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    second_payment_utxo = await_payment(buyer.address, second_payment_txn, blockfrost_api)

    for wl_pass_onchain in wl_passes_onchain:
        assert not whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should NO LONGER be on the whitelist"
    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )
    for wl_pass_onchain in wl_passes_onchain:
        assert not whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should NO LONGER be on the whitelist"

    created_assets = blockfrost_api.get_assets(policy.id)
    assert len(created_assets) == 2, f"Test should NOT create third+ assets under {policy.id}: {created_assets}"
    for created_asset in created_assets:
        assert int(created_asset['quantity']) == 1, f"Test should NOT create more quantities of asset {created_asset}"

    drain_payment = lovelace_in(profit_utxo)
    drain_txn = send_money(
            [funder],
            drain_payment,
            profit,
            [profit_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, drain_txn, blockfrost_api)

    burn_assets = [hex_to_asset_name(created_asset['asset'][56:]) for created_asset in created_assets]
    burn_payment = lovelace_in(second_payment_utxo) - MIN_UTXO_PAYMENT
    burn_txn = burn_and_reclaim_tada(
            burn_assets,
            policy,
            policy_keys,
            EXPIRATION,
            funder,
            burn_payment,
            buyer,
            [second_payment_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    burn_utxo = await_payment(buyer.address, burn_txn, blockfrost_api)

    assert policy_is_empty(policy, blockfrost_api), f"Burned asset successfully but {policy.id} has remaining_assets"

    wl_burn_payment = lovelace_in(burn_utxo)
    wl_burn_txn = burn_and_reclaim_tada(
            wl_passes,
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            wl_burn_payment,
            buyer,
            [burn_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, wl_burn_txn, blockfrost_api)

    refund_utxo = await_payment(buyer.address, None, blockfrost_api)
    refund_payment = lovelace_in(refund_utxo)
    assert refund_payment > MINT_PRICE, f"Expecting refund greater than {MINT_PRICE} instead found {refund_utxo}"
    refund_txn = send_money(
            [funder],
            refund_payment,
            buyer,
            [refund_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, refund_txn, blockfrost_api)

def test_mints_correct_number_with_same_utxo(request, vm_test_config, blockfrost_api, cardano_cli):
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_amt = (MINT_PRICE * 5) + PADDING
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_passes = ["WildTangz WL 1", "WildTangz WL 2", "WildTangz WL 3"]
    wl_passes_onchain = [f"{wl_policy.id}{asset_name_hex(wl_pass)}" for wl_pass in wl_passes]
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets(wl_passes, wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = SingleUseWhitelist(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    for wl_pass_onchain in wl_passes_onchain:
        assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    mint_payment = MINT_PRICE + PADDING
    payment_txn = send_money(
            [payment],
            mint_payment,
            buyer,
            [wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)
    buyer_send_to_self_utxo = await_payment(buyer.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            SINGLE_VEND_MAX,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    asset_names = ["WildTangz 1", "WildTangz 2", "WildTangz 3"]
    create_asset_files(asset_names, policy, request, vm_test_config.metadata_dir)

    exclusions = set()
    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            exclusions
    )
    profit_utxo = await_payment(profit.address, None, blockfrost_api)
    profit_txn = blockfrost_api.get_txn(profit_utxo.hash)
    profit_expected = (MINT_PRICE) - Mint.RebateCalculator.calculate_rebate_for(1, 1, 1 * len(asset_names[0])) - int(profit_txn['fees'])
    minted_utxo = await_payment(buyer.address, profit_utxo.hash, blockfrost_api)

    num_still_whitelisted = 0
    for wl_pass_onchain in wl_passes_onchain:
        if whitelist.is_whitelisted(wl_pass_onchain):
            num_still_whitelisted += 1
    assert num_still_whitelisted == 2, f"There should still be 2 assets on the whitelist"

    mint_payment = (MINT_PRICE * 3) + PADDING
    second_payment_txn = send_money(
            [payment],
            mint_payment,
            buyer,
            [buyer_send_to_self_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    second_payment_utxo = await_payment(buyer.address, second_payment_txn, blockfrost_api)

    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            exclusions
    )
    second_profit_utxo = await_payment(profit.address, None, blockfrost_api, exclusions=[profit_utxo])
    second_profit_txn = blockfrost_api.get_txn(second_profit_utxo.hash)
    second_profit_expected = (MINT_PRICE * 2) - Mint.RebateCalculator.calculate_rebate_for(1, 2, 2 * len(asset_names[0])) - int(second_profit_txn['fees'])
    second_minted_utxo = await_payment(buyer.address, second_profit_utxo.hash, blockfrost_api)

    for wl_pass_onchain in wl_passes_onchain:
        assert not whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should NO LONGER be on the whitelist"

    profit_actual = lovelace_in(profit_utxo) + lovelace_in(second_profit_utxo)
    assert profit_actual == (profit_expected + second_profit_expected), f"Expected {(profit_expected + second_profit_expected)}, but actual was {profit_actual}"

    created_assets = blockfrost_api.get_assets(policy.id)
    assert len(created_assets) == 3, f"Test did not create 3 assets under {policy.id}: {created_assets}"
    for created_asset in created_assets:
        minted_assetid = created_asset['asset']
        asset_name = hex_to_asset_name(minted_assetid[56:])
        assert minted_assetid.startswith(policy.id), f"Minted asset {minted_assetid} does not belong to policy {policy.id}"
        assert asset_name in asset_names, f"Minted asset {minted_assetid} does not have hex name {asset_name}"

    drain_txn = send_money(
            [funder],
            profit_actual,
            profit,
            [profit_utxo, second_profit_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, drain_txn, blockfrost_api)

    burn_assets = [hex_to_asset_name(created_asset['asset'][56:]) for created_asset in created_assets]
    burn_payment = lovelace_in(minted_utxo) + lovelace_in(second_minted_utxo) + lovelace_in(second_payment_utxo) - MINT_PRICE
    burn_txn = burn_and_reclaim_tada(
            burn_assets,
            policy,
            policy_keys,
            EXPIRATION,
            funder,
            burn_payment,
            buyer,
            [minted_utxo, second_minted_utxo, second_payment_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    burn_utxo = await_payment(buyer.address, burn_txn, blockfrost_api)

    assert policy_is_empty(policy, blockfrost_api), f"Burned asset successfully but {policy.id} has remaining_assets"

    wl_burn_payment = lovelace_in(burn_utxo)
    wl_burn_txn = burn_and_reclaim_tada(
            wl_passes,
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            wl_burn_payment,
            buyer,
            [burn_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, wl_burn_txn, blockfrost_api)

def test_mints_correct_number_for_unlimited_use(request, vm_test_config, blockfrost_api, cardano_cli):
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_inputs = find_min_utxos_for_txn(WL_REBATE, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            WL_REBATE,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_pass = "WildTangz WL 1"
    wl_pass_onchain = f"{wl_policy.id}{asset_name_hex(wl_pass)}"
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets([wl_pass], wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = UnlimitedWhitelist(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_amt = (MINT_PRICE * 4) + PADDING
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    buyer_utxo = await_payment(buyer.address, funding_request_txn, blockfrost_api)

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    payment_txn = send_money(
            [payment],
            MINT_PRICE * 2 + PADDING,
            buyer,
            [buyer_utxo, wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            SINGLE_VEND_MAX,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    asset_names = ["WildTangz 1", "WildTangz 2", "WildTangz 3", "WildTangz 4"]
    create_asset_files(asset_names, policy, request, vm_test_config.metadata_dir)

    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )

    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should STILL be on the whitelist"

    profit_utxo = await_payment(profit.address, None, blockfrost_api)
    profit_txn = blockfrost_api.get_txn(profit_utxo.hash)
    profit_expected = (2 * MINT_PRICE) - Mint.RebateCalculator.calculate_rebate_for(1, 2, 2 * len(asset_names[0])) - int(profit_txn['fees'])
    profit_actual = lovelace_in(profit_utxo)
    assert profit_actual == profit_expected, f"Expected {profit_expected}, but actual was {profit_actual}"

    minted_utxo = await_payment(buyer.address, profit_utxo.hash, blockfrost_api)
    created_assets = blockfrost_api.get_assets(policy.id)
    assert len(created_assets) == 2, f"Test did not create 2 assets under {policy.id}: {created_assets}"
    assert lovelace_in(minted_utxo) < MINT_PRICE, f"Buyer requested two and should have received small rebate"

    for created_asset in created_assets:
        minted_assetid = created_asset['asset']
        asset_name = hex_to_asset_name(minted_assetid[56:])
        assert lovelace_in(minted_utxo, policy=policy, asset_name=asset_name) == 1, f"Buyer does not have {asset_name} in {minted_utxo}"
        assert minted_assetid.startswith(policy.id), f"Minted asset {minted_assetid} does not belong to policy {policy.id}"
        assert asset_name in asset_names, f"Minted asset {minted_assetid} does not have hex name {asset_name}"

    minted_asset = blockfrost_api.get_asset(minted_assetid)
    assert minted_asset, f"Could not retrieve {minted_assetid} from the blockchain"
    expected_metadata = metadata_json(request, asset_filename(asset_name))[asset_name]
    assert minted_asset['onchain_metadata'] == expected_metadata, f"Mismatch in metadata: {minted_asset}"

    mint_sendself_utxo = await_payment(buyer.address, payment_txn, blockfrost_api)
    second_payment_txn = send_money(
            [payment],
            MINT_PRICE + PADDING,
            buyer,
            [minted_utxo, mint_sendself_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    second_payment_utxo = await_payment(buyer.address, second_payment_txn, blockfrost_api)

    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should STILL be on the whitelist"
    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )
    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should STILL be on the whitelist"
    second_profit_utxo = await_payment(profit.address, None, blockfrost_api, exclusions=[profit_utxo])
    second_mint_utxo = await_payment(buyer.address, second_profit_utxo.hash, blockfrost_api)

    created_assets = blockfrost_api.get_assets(policy.id)
    assert len(created_assets) == 3, f"Test SHOULD have created a third asset under {policy.id}: {created_assets}"
    created_asset_names = []
    for created_asset in created_assets:
        minted_assetid = created_asset['asset']
        asset_name = hex_to_asset_name(minted_assetid[56:])
        created_asset_names.append(asset_name)
        quantity = int(created_asset['quantity'])
        assert quantity == 1, f"{created_asset} should have quantity of 1, not {quantity}"

    drain_payment = lovelace_in(profit_utxo) + lovelace_in(second_profit_utxo)
    drain_txn = send_money(
            [funder],
            drain_payment,
            profit,
            [profit_utxo, second_profit_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, drain_txn, blockfrost_api)

    burn_payment = lovelace_in(second_payment_utxo) - PADDING
    burn_txn = burn_and_reclaim_tada(
            created_asset_names,
            policy,
            policy_keys,
            EXPIRATION,
            funder,
            burn_payment,
            buyer,
            [second_payment_utxo, second_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    burn_utxo = await_payment(buyer.address, burn_txn, blockfrost_api)

    assert policy_is_empty(policy, blockfrost_api), f"Burned asset successfully but {policy.id} has remaining_assets"

    wl_burn_payment = lovelace_in(burn_utxo)
    wl_burn_txn = burn_and_reclaim_tada(
            [wl_pass],
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            wl_burn_payment,
            buyer,
            [burn_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )

def test_respects_single_vend_max_for_unlimited_use(request, vm_test_config, blockfrost_api, cardano_cli):
    single_vend_cap = 3
    buyer = Address.new(
            vm_test_config.buyers_dir,
            'buyer',
            get_network_magic()
    )

    funder = get_funder_address(request)
    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_inputs = find_min_utxos_for_txn(WL_REBATE, funding_utxos, funder.address)
    wl_funding_request_txn = send_money(
            [buyer],
            WL_REBATE,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    wl_buyer_utxo = await_payment(buyer.address, wl_funding_request_txn, blockfrost_api)

    wl_policy_keys = KeyPair.new(vm_test_config.policy_dir, 'wl_policy')
    wl_policy = new_policy_for(wl_policy_keys, vm_test_config.policy_dir, 'wl_policy.script', expiration=WL_EXPIRATION)

    wl_pass = "WildTangz WL 1"
    wl_pass_onchain = f"{wl_policy.id}{asset_name_hex(wl_pass)}"
    wl_selfpayment = lovelace_in(wl_buyer_utxo)
    wl_txn = mint_assets([wl_pass], wl_policy, wl_policy_keys, WL_EXPIRATION, buyer, wl_selfpayment, buyer, [wl_buyer_utxo], cardano_cli, blockfrost_api, vm_test_config.root_dir)
    wl_mint_utxo = await_payment(buyer.address, wl_txn, blockfrost_api)

    initialize_asset_wl(vm_test_config.whitelist_dir, vm_test_config.consumed_dir, wl_policy, request, blockfrost_api)
    whitelist = UnlimitedWhitelist(vm_test_config.whitelist_dir, vm_test_config.consumed_dir)
    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should be on the whitelist"

    funding_utxos = blockfrost_api.get_utxos(funder.address, [])
    print('Funder address currently has: ', sum([lovelace_in(funding_utxo) for funding_utxo in funding_utxos]))
    funding_amt = (MINT_PRICE * 5) + (PADDING * 5)
    funding_inputs = find_min_utxos_for_txn(funding_amt, funding_utxos, funder.address)
    funding_request_txn = send_money(
            [buyer],
            funding_amt,
            funder,
            funding_inputs,
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    buyer_utxo = await_payment(buyer.address, funding_request_txn, blockfrost_api)

    payment = Address.new(
            vm_test_config.payees_dir,
            'payment',
            get_network_magic()
    )
    payment_amt = lovelace_in(buyer_utxo) + lovelace_in(wl_mint_utxo)
    payment_txn = send_money(
            [payment],
            (MINT_PRICE * 5 + PADDING),
            buyer,
            [buyer_utxo, wl_mint_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    payment_utxo = await_payment(payment.address, payment_txn, blockfrost_api)
    buyer_send_to_self_utxo = await_payment(buyer.address, payment_txn, blockfrost_api)

    policy_keys = KeyPair.new(vm_test_config.policy_dir, 'policy')
    policy = new_policy_for(policy_keys, vm_test_config.policy_dir, 'policy.script', expiration=EXPIRATION)
    mint = Mint(
            policy.id,
            MINT_PRICE,
            DONATION_AMT,
            vm_test_config.metadata_dir,
            policy.script_file_path,
            policy_keys.skey_path,
            whitelist
    )
    profit = Address.new(
            vm_test_config.payees_dir,
            'profit',
            get_network_magic()
    )
    nft_vending_machine = NftVendingMachine(
            payment.address,
            payment.keypair.skey_path,
            profit.address,
            VEND_RANDOMLY,
            single_vend_cap,
            mint,
            blockfrost_api,
            cardano_cli,
            mainnet=get_mainnet_env()
    )
    nft_vending_machine.validate()

    asset_names = ["WildTangz 1", "WildTangz 2", "WildTangz 3", "WildTangz 4", "WildTangz 5"]
    create_asset_files(asset_names, policy, request, vm_test_config.metadata_dir)

    nft_vending_machine.vend(
            vm_test_config.root_dir,
            vm_test_config.locked_dir,
            vm_test_config.txn_metadata_dir,
            set()
    )

    assert whitelist.is_whitelisted(wl_pass_onchain), f"{wl_pass_onchain} should STILL be on the whitelist"

    profit_utxo = await_payment(profit.address, None, blockfrost_api)
    profit_txn = blockfrost_api.get_txn(profit_utxo.hash)
    user_rebate = Mint.RebateCalculator.calculate_rebate_for(1, 3, 3 * len(asset_names[0]))
    profit_expected = (3 * MINT_PRICE) - user_rebate - int(profit_txn['fees'])
    profit_actual = lovelace_in(profit_utxo)
    assert profit_actual == profit_expected, f"Expected {profit_expected}, but actual was {profit_actual}"

    minted_utxo = await_payment(buyer.address, profit_utxo.hash, blockfrost_api)
    created_assets = blockfrost_api.get_assets(policy.id)
    assert len(created_assets) == 3, f"Test did not create 3 assets under {policy.id}: {created_assets}"
    assert lovelace_in(minted_utxo) > (MINT_PRICE * 2), f"Buyer requested 5 but should only have gotten 3"

    created_asset_names = []
    for created_asset in created_assets:
        minted_assetid = created_asset['asset']
        asset_name = hex_to_asset_name(minted_assetid[56:])
        created_asset_names.append(asset_name)
        assert lovelace_in(minted_utxo, policy=policy, asset_name=asset_name) == 1, f"Buyer does not have {asset_name} in {minted_utxo}"
        assert minted_assetid.startswith(policy.id), f"Minted asset {minted_assetid} does not belong to policy {policy.id}"
        assert asset_name in asset_names, f"Minted asset {minted_assetid} does not have hex name {asset_name}"
        assert int(created_asset['quantity']) == 1, f"{created_asset} should have quantity of 1, not {quantity}"

    assert len(os.listdir(vm_test_config.metadata_dir)) == 2, "Should have two metadata files remaining to use after mint"

    drain_payment = lovelace_in(profit_utxo)
    drain_txn = send_money(
            [funder],
            drain_payment,
            profit,
            [profit_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, drain_txn, blockfrost_api)

    burn_txn = burn_and_reclaim_tada(
            created_asset_names,
            policy,
            policy_keys,
            EXPIRATION,
            funder,
            lovelace_in(minted_utxo),
            buyer,
            [minted_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, burn_txn, blockfrost_api)

    assert policy_is_empty(policy, blockfrost_api), f"Burned asset successfully but {policy.id} has remaining_assets"

    wl_burn_txn = burn_and_reclaim_tada(
            [wl_pass],
            wl_policy,
            wl_policy_keys,
            WL_EXPIRATION,
            funder,
            lovelace_in(buyer_send_to_self_utxo),
            buyer,
            [buyer_send_to_self_utxo],
            cardano_cli,
            blockfrost_api,
            vm_test_config.root_dir
    )
    await_payment(funder.address, wl_burn_txn, blockfrost_api)
