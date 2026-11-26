const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const dashboard = fs.readFileSync(path.join(__dirname, '../../src/xian/dashboard/static/index.html'), 'utf8');
const scripts = Array.from(dashboard.matchAll(/<script>([\s\S]*?)<\/script>/g), match => match[1]);
const recipient = 'a'.repeat(64);
const validator = 'b'.repeat(64);

const nextTurn = () => new Promise(resolve => setImmediate(resolve));

function openExplorer(route, respond) {
    const elements = new Map();
    function element(id) {
        if (!elements.has(id)) {
            elements.set(id, {
                listeners: new Map(),
                addEventListener(type, listener) { this.listeners.set(type, listener); },
                classList: { add() {}, remove() {}, toggle() {} },
                dataset: {},
                setAttribute(key, value) { this[key] = value; },
                scrollIntoView() {},
                style: {},
            });
        }
        return elements.get(id);
    }
    const handlers = new Map();
    const historyEntries = [];
    const window = { addEventListener: (type, handler) => handlers.set(type, handler) };
    const location = new URL(route ? '/explorer/' + route : '/explorer', 'http://localhost');
    vm.runInNewContext(scripts.join('\n'), {
        document: {
            documentElement: { dataset: {} },
            getElementById: element,
            querySelector: element,
            querySelectorAll: selector => {
                const groups = {
                    '.tab': ['tab', ['dashboard', 'explorer']],
                    '[data-explorer-view]': ['explorerView', ['blocks', 'contracts', 'addresses', 'events']],
                };
                if (!groups[selector]) return [];
                const [attribute, names] = groups[selector];
                return names.map(name => {
                    const control = element(name);
                    control.dataset[attribute] = name;
                    return control;
                });
            },
            addEventListener() {},
        },
        window,
        location,
        history: {
            pushState(_state, _title, url) {
                location.href = new URL(url, location).href;
                historyEntries.push(location.pathname);
            },
        },
        URL,
        fetch: async url => {
            // Keep dashboard initialization paused after it opens the requested detail page.
            if (url.pathname === '/api/config') return new Promise(() => {});
            const response = await respond(url.pathname);
            return { ok: true, json: async () => response };
        },
    });
    return { element, location, historyEntries, popstate: handlers.get('popstate') };
}

async function renderExplorer(route, response) {
    const { element } = openExplorer(route, pathname => {
        assert.equal(pathname, `/api/${route}`);
        return response;
    });
    await nextTurn();
    const detail = element('explorer-detail');
    return detail.innerHTML;
}

function renderTransaction(receipt) {
    return renderExplorer(`tx/${'c'.repeat(64)}`, {
        height: '1',
        tx_result: { code: receipt.status, data_decoded: receipt },
    });
}

test('shows exact delegation pool credits and developer rewards from receipt records', async () => {
    const html = await renderTransaction({
        status: 0,
        rewards: { delegator_pool_reward: { validators: { __fixed__: '0.5' } } },
        reward_records: [
            { type: 'delegator_pool_reward', recipient_key: 'validators', validator_key: validator, value: { __fixed__: '0.123456789012345678901234567891' } },
            { type: 'developer_reward', recipient_key: recipient, source_contract: 'con_tea', value: { __fixed__: '0.25' } },
        ],
    });
    assert.match(html, /Delegator Pool/);
    assert.match(html, /href="\/explorer\/contract\/validators"/);
    assert.match(html, new RegExp(`href="/explorer/address/${validator}"`));
    assert.match(html, />0\.123456789012345678901234567891</);
    assert.match(html, /Developer/);
    assert.match(html, /href="\/explorer\/contract\/con_tea"/);
    assert.match(html, new RegExp(`href="/explorer/address/${recipient}"`));
    assert.match(html, />0\.25</);
    assert.doesNotMatch(html, />0\.5</);
});

test('shows grouped rewards on a failed legacy transaction', async () => {
    const html = await renderTransaction({
        status: 1,
        rewards: { validator_reward: { [recipient]: { __fixed__: '0.125' } }, developer_reward: {} },
        reward_records: [],
    });
    assert.match(html, /Failed \(code 1\)/);
    assert.match(html, />Validator</);
    assert.match(html, />0\.125</);
    assert.doesNotMatch(html, />Developer</);
});

test('keeps older array rewards including zero and unspecified amounts', async () => {
    const html = await renderTransaction({
        status: 0,
        rewards: [{ key: recipient, value: 0 }, { [validator]: '0.25' }, { key: 'unknown' }],
    });
    assert.match(html, new RegExp(`href="/explorer/address/${recipient}"`));
    assert.match(html, />0<\/td>/);
    assert.match(html, new RegExp(`href="/explorer/address/${validator}"`));
    assert.match(html, />0\.25<\/td>/);
    assert.match(html, /href="\/explorer\/address\/unknown"[^>]*>unknown<\/a><\/td><td[^>]*>—<\/td>/);
});

test('omits the rewards section for an unpaid transaction', async () => {
    const html = await renderTransaction({ status: 0, rewards: null, reward_records: null });
    assert.match(html, /Transaction Result/);
    assert.doesNotMatch(html, />Rewards</);
});

test('shows exact indexed reward totals in address and contract summaries', async () => {
    const summary = { total_rewards: { __fixed__: '0.123456789012345678901234567891' } };
    const addressHtml = await renderExplorer(`address/${recipient}`, { available: true, developer_rewards: summary });
    const contractHtml = await renderExplorer('contract/con_tea', { summary });
    assert.match(addressHtml, /Developer Rewards<\/span><span class="data-val">0\.123456789012345678901234567891</);
    assert.match(contractHtml, /Generated Rewards<\/span><span class="data-val">0\.123456789012345678901234567891</);
});

test('restores the explorer tab from the history URL while dropping a stale block result', async () => {
    let finishBlock;
    const pendingBlock = new Promise(resolve => { finishBlock = resolve; });
    const requests = [];
    const { element, location, historyEntries, popstate } = openExplorer('block/3', pathname => {
        requests.push(pathname);
        if (pathname === '/api/block/3') return pendingBlock;
        if (pathname === '/api/block_results/3') return { txs_results: [] };
        return { items: [] };
    });
    assert.match(element('explorer-detail').innerHTML, /Loading block 3/);

    location.pathname = '/explorer/contracts';
    popstate({ state: { view: 'list' } });
    finishBlock({ block: { header: { height: '3' } } });
    await nextTurn();

    assert.equal(element('explorer')['aria-current'], 'page');
    assert.equal(element('dashboard')['aria-current'], 'false');
    assert.equal(element('contracts')['aria-pressed'], 'true');
    assert.equal(element('blocks')['aria-pressed'], 'false');
    assert.deepEqual(historyEntries, []);
    assert.ok(requests.includes('/api/contracts'));
    assert.equal(element('explorer-detail').innerHTML, '');
    assert.equal(element('explorer-list').style.display, '');
});

test('links the block height and canonical transaction hash in explorer tables', async () => {
    const { element, location, popstate } = openExplorer('', pathname => {
        if (pathname === '/api/status') return { sync_info: { latest_block_height: '12' } };
        if (pathname === '/api/blockchain') return { block_metas: [
            { header: { height: '12' }, block_id: { hash: validator }, num_txs: 2 },
            { header: { height: '11' }, block_id: { hash: validator } },
        ] };
        if (pathname === '/api/block/12') return {
            block: { header: { height: '12' } },
            decoded_txs: [{ tx_hash: recipient }, {}],
        };
        if (pathname === '/api/block_results/12') return { txs_results: [] };
        throw new Error('Unexpected request ' + pathname);
    });
    await nextTurn();
    const blocks = element('explorer-blocks').innerHTML;
    assert.match(blocks, /<a href="\/explorer\/block\/12"[^>]*>12<\/a><\/td>\s*<td>2<\/td>/);
    assert.match(blocks, /<a href="\/explorer\/block\/11"[^>]*>11<\/a><\/td>\s*<td>0<\/td>/);
    location.pathname = '/explorer/block/12';
    popstate({});
    await nextTurn();
    const detail = element('explorer-detail').innerHTML;
    assert.match(detail, new RegExp('<a [^>]*href="/explorer/tx/' + recipient + '"[^>]*>'));
    assert.match(detail, /<td>2<\/td>/);
    assert.doesNotMatch(detail, /href="\/explorer\/tx\/(null|undefined)"/);
});

test('renders genesis as contract provenance and links a real creation receipt', async () => {
    const genesis = await renderExplorer('contract/token_factory', {
        summary: { last_tx_hash: 'GENESIS', submitted_at_block: 0 },
    });
    assert.match(genesis, /Creation Tx<\/span><span class="data-val">Chain genesis<\/span>/);
    assert.doesNotMatch(genesis, /href="\/explorer\/(tx\/GENESIS|block\/0)"/);

    const deployed = await renderExplorer('contract/con_tea', {
        summary: { last_tx_hash: recipient, submitted_at_block: 12 },
    });
    assert.ok(deployed.includes('href="/explorer/tx/' + recipient + '"'));
    assert.ok(deployed.includes('href="/explorer/block/12"'));
});

test('shows the indexed genesis entry without a transaction link in address history', async () => {
    const html = await renderExplorer('address/sys', {
        available: true,
        transactions: [{ tx_hash: 'GENESIS', block_height: 0 }],
    });
    assert.match(html, /<tr><td>—<\/td><td>Chain genesis<\/td>/);
    assert.doesNotMatch(html, /href="\/explorer\/tx\/GENESIS"/);
});

test('switches explorer categories when search opens an address, contract, or transaction', async () => {
    const { element, location } = openExplorer('', pathname => {
        if (pathname === '/api/status') return { sync_info: { latest_block_height: '12' } };
        if (pathname === '/api/blockchain') return { block_metas: [] };
        if (pathname === '/api/tx/' + recipient) return { error: 'transaction not found' };
        if (pathname === '/api/address/' + recipient) return { available: true };
        if (pathname === '/api/contract/con_tea') return { name: 'con_tea' };
        if (pathname === '/api/tx/' + validator) return { hash: validator, tx_result: {} };
        throw new Error('Unexpected request ' + pathname);
    });
    await nextTurn();
    for (const [query, route, selected] of [
        [recipient, 'address/' + recipient, 'addresses'],
        ['con_tea', 'contract/con_tea', 'contracts'],
        [validator, 'tx/' + validator, 'blocks'],
    ]) {
        element('search-input').value = query;
        await element('search-btn').listeners.get('click')();
        await nextTurn();
        assert.equal(location.pathname, '/explorer/' + route);
        const categories = ['blocks', 'contracts', 'addresses', 'events'];
        assert.deepEqual(categories.filter(name => element(name)['aria-pressed'] === 'true'), [selected]);
    }
});
