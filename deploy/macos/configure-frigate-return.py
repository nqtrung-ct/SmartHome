from pathlib import Path
import yaml

path = Path('/Volumes/Data/frigate-return/config/config.yml')
config = yaml.safe_load(path.read_text(encoding='utf-8'))
config['detectors'] = {'apple_silicon': {'type': 'zmq', 'endpoint': 'tcp://host.docker.internal:5555'}}
config['database'] = {'path': '/frigate-db/frigate.db'}
path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
path.chmod(0o600)
