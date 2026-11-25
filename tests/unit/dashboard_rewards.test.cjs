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
                addEventListener() {},
                classList: { add() {}, remove() {} },
                scrollIntoView() {},
                style: {},
            });
        }
        return elements.get(id);
    }
    const handlers = new Map();
    const window = { addEventListener: (type, handler) => handlers.set(type, handler) };
    const location = new URL(`http://localhost/explorer/${route}`);
    vm.runInNewContext(scripts.join('\n'), {
        document: {
            documentElement: { dataset: {} },
            getElementById: element,
            querySelector: element,
            querySelectorAll: () => [],
            addEventListener() {},
        },
        window,
        location,
        URL,
        fetch: async url => {
            // Keep dashboard initialization paused after it opens the requested detail page.
            if (url.pathname === '/api/config') return new Promise(() => {});
            const response = await respond(url.pathname);
            return { ok: true, json: async () => response };
        },
    });
    return { element, location, popstate: handlers.get('popstate') };
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
    const { element, location, popstate } = openExplorer('block/3', pathname => {
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

    assert.ok(requests.includes('/api/contracts'));
    assert.equal(element('explorer-detail').innerHTML, '');
    assert.equal(element('explorer-list').style.display, '');
});
