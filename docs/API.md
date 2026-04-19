# Xian-Core API Documentation

## Overview
This document provides details about the API endpoints available in the Xian-Core application.

## Endpoints

### 1. Transactions
#### Broadcast Transaction
Broadcasts a signed transaction to the network.

##### Request
- Method: GET
- URL: `/broadcast_tx_sync`
- Query Parameters:
  - `tx`: The transaction to broadcast (hex-encoded JSON string)

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The result of the broadcast
    - `code`: The result code (0 if successfully passed CheckTx)
    - `data`: Hex-encoded JSON string with CheckTx information
    - `hash`: The transaction hash
    - `log`: The transaction log
    - `codespace`: The transaction codespace

#### Get Transaction
Retrieves a transaction by its hash.

##### Request
- Method: GET
- URL: `/tx`
- Query Parameters:
  - `hash`: The transaction hash (prepend with `0x`)
  - `prove`: Whether to include a proof of the transaction (default: false)

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The transaction data
    - `hash`: The transaction hash
    - `height`: The block height
    - `index`: The transaction index
    - `tx_result`: The transaction result
      - `code`: The transaction result code
      - `data`: Hex-encoded JSON string with transaction data
      - `log`: The transaction log
      - `codespace`: The transaction codespace
    - `tx`: The transaction data
    - `proof`: The transaction proof
    - `proof_height`: The proof height

### 2. Wallets
#### Get Next Nonce
Retrieves the next nonce for a given address.

##### Request
- Method: GET
- URL: `/abci_query?path="/get_next_nonce/<address>"`

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The query result
    - `response`: The response data
      - `value`: Hex-encoded nonce

### 3. Get Values from State
#### Query State
Queries the data at a given path in the state store

##### Request
- Method: GET
- URL: `/abci_query?path="/get/<contract>.<hash>:<key>"`
- Alternate URL: `/abci_query?path="/get/<contract>.<variable>"`

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The query result
    - `response`: The response data
      - `value`: Hex-encoded value

#### Keys in a Hash
Retrieves keys in a given hash with bounded pagination.

##### Request
- Method: GET
- URL: `/abci_query?path="/keys/<contract>.<hash>/limit=100/after=<last-key>"`

##### Query Parameters
- `limit`: Optional page size, capped server-side at `200`
- `after`: Optional key suffix from the previous page's `next_after`

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The query result
    - `response`: The response data
      - `value`: Hex-encoded JSON object with:
        - `prefix`: The queried hash prefix
        - `items`: List of key suffixes for the current page
        - `limit`: Effective page size
        - `after`: Supplied cursor, if any
        - `next_after`: Cursor for the next page, if more results remain
        - `has_more`: Whether more matching keys remain after this page

#### Deployed Contracts
Retrieves the deployed contracts

##### Request
- Method: GET
- URL: `/abci_query?path="/contracts"`

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The query result
    - `response`: The response data
      - `value`: Hex-encoded JSON string with list of contracts

#### Contract Code
Retrieves the code for a given contract

##### Request
- Method: GET
- URL: `/abci_query?path="/contract/<contract>"`

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The query result
    - `response`: The response data
      - `value`: Hex-encoded code

#### Contract Methods
Retrieves the methods for a given contract

##### Request
- Method: GET
- URL: `/abci_query?path="/contract_methods/<contract>"`

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The query result
    - `response`: The response data
      - `value`: Hex-encoded JSON string with method data

### 4. Services
#### Estimate Chi
Estimates the number of chi required for a given transaction

##### Request
- Method: GET
- URL: `/abci_query?path="/estimate_chi/<hex-encoded-signed-transaction>"`

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The query result
    - `response`: The response data
      - `value`: Hex-encoded JSON string with chi estimate and tx result

#### Lint Code
Lints the code for a given contract

##### Request
- Method: GET
- URL: `/abci_query?path="/lint/<base64-encoded-urlsafe-code>"`

##### Response
- Content-Type: application/json
- Body: JSON object with the following fields:
  - `jsonrpc`: The JSON-RPC version
  - `id`: The request ID
  - `result`: The query result
    - `response`: The response data
      - `value`: Hex-encoded JSON string with lint result
