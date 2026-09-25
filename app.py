from __future__ import annotations
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
from src.http_api import make_handler
from src.initiation_service import InitiationService, seed_demo
from src.repository import Repository
from src.service import Service
def parse_args():
    parser=argparse.ArgumentParser(description='建筑抗震鉴定与加固排序/加固立项台')
    parser.add_argument("--db",default="./data.db",help="SQLite数据库路径")
    parser.add_argument("--port",type=int,default=8317,help="HTTP端口")
    parser.add_argument("--host",default="127.0.0.1",help="监听地址")
    parser.add_argument("--seed",action="store_true",help="首次启动灌入立项台演示数据")
    return parser.parse_args()
def main():
    args=parse_args(); repository=Repository(args.db); service=Service(repository)
    initiation=InitiationService(repository)
    if args.seed: seed_demo(initiation)
    server=ThreadingHTTPServer((args.host,args.port),make_handler(service,str(Path(__file__).resolve().parent/"static"),initiation))
    print(f"listening on http://{args.host}:{args.port}")
    print(f"立项台: http://{args.host}:{args.port}/initiation")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); repository.close()
if __name__=="__main__": main()
