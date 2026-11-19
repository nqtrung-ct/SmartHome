"""Adjust a copied local configuration for the Mac deployment."""
from pathlib import Path
import yaml

path = Path('/Volumes/Data/Projects/SmartHome/config.yaml')
config = yaml.safe_load(path.read_text(encoding='utf-8'))
config['server']['data_dir'] = '/Volumes/Data/Projects/SmartHome/data'
config['llm']['base_url'] = 'http://192.168.1.99:11434'
config['llm']['ollama_url'] = 'http://192.168.1.99:11434'
config['stt']['engine'] = 'sherpa'
config['frigate']['base_url'] = 'http://127.0.0.1:5001'
config['frigate']['config_path'] = '/Volumes/Data/frigate-return/config/config.yml'
config['frigate']['docker_command'] = '/Users/ti/.orbstack/bin/docker'
path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
path.chmod(0o600)
