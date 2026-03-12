from argparse import ArgumentParser
from pathlib import Path
import json
from xian.node_setup import build_priv_validator_key

"""
Generate priv_validator_key.json file for your validator node
"""


class ValidatorGen:
    def __init__(self):
        parser = ArgumentParser(description='Validator File Generator')
        parser.add_argument(
            '--validator_privkey',
            type=str,
            help="Validator's private key",
            required=True
        )
        parser.add_argument(
            '--output-path',
            type=Path,
            default=None,
            help="Path to save generated file"
        )
        self.args = parser.parse_args()

    def main(self):
        output_path = Path(self.args.output_path) if self.args.output_path else Path.cwd()
        output_file = output_path / Path('priv_validator_key.json')

        if len(self.args.validator_privkey) != 64:
            print('Validator private key must be 64 characters')
            return
        
        keys = build_priv_validator_key(self.args.validator_privkey)
        keys.pop("_private_key_hex", None)

        with open(output_file, 'w') as f:
            f.write(json.dumps(keys, indent=2))


if __name__ == '__main__':
    ValidatorGen().main()
