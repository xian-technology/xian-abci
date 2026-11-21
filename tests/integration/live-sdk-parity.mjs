import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const { Ed25519Signer, RpcError, TxNotFoundError, XianClient, encodeRuntime, prepareTransaction } = await import(
  pathToFileURL(resolve("packages/client/dist/index.js")).href
);
const { rpc, contract, hash, owner, key } = JSON.parse(readFileSync(0, "utf8"));
const client = new XianClient({ rpcUrl: rpc });
const signer = new Ed25519Signer(key);
const receipt = await client.waitForTx(hash, { timeoutMs: 10_000 });
if (!receipt.success || !receipt.transaction) {
  throw new Error(`Failed transaction ${hash}`);
}
const amount = await client.getState(contract, "saved_amount");
const receiptResult = receipt.execution.result;
const echoed = await client.sendTx({
  sender: signer.address,
  signer,
  contract,
  function: "echo",
  kwargs: { value: [amount, receiptResult[1]] },
  chi: 20_000,
  waitForTx: true,
  timeoutMs: 10_000
});
if (!echoed.receipt?.success) {
  throw new Error("the JavaScript replay transaction did not execute successfully");
}
const missingTx = await client.getTx("CD".repeat(32)).then(
  () => { throw new Error("unknown hash returned a receipt"); },
  (error) => {
    if (!(error instanceof RpcError || error instanceof TxNotFoundError)) throw error;
    return error.name;
  }
);
console.log(encodeRuntime({
  contract,
  receipt: {
    hash: receipt.txHash,
    wire: prepareTransaction(receipt.transaction).txHash,
    result: receiptResult
  },
  memo: await client.getState(contract, "memo"),
  amount,
  echoed: echoed.receipt.execution.result,
  balance: await client.getBalance(owner, { contract }),
  allowance: await client.token(contract).allowance(owner, "con_spender"),
  missingTx
}));
