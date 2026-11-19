import runpy
import sys
import yaml

with open('config.yaml', encoding='utf-8') as f:
    token = yaml.safe_load(f)['server']['admin_token']
sys.argv = ['scripts/smoke_test.py', 'http://127.0.0.1:8124', token]
runpy.run_path(sys.argv[0], run_name='__main__')
