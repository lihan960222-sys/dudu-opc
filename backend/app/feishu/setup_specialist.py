"""Add a specialist application to an existing private gateway config, without starting it."""
import argparse
import ctypes
from ctypes import wintypes
import getpass
import json
import os
from pathlib import Path
import re
import tempfile

APP = re.compile(r'cli_[A-Za-z0-9]+')
SECRET_ENV = re.compile(r'FEISHU_[A-Z][A-Z0-9_]*_SECRET')


def dpapi(data, *, unlock):
    if os.name != 'nt':
        raise ValueError('Windows DPAPI 仅在 Windows 可用。其他系统请用 --env-only。')
    class Blob(ctypes.Structure):
        _fields_ = [('size',wintypes.DWORD),('ptr',ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    source,result = Blob(len(data),buffer),Blob()
    crypt = ctypes.WinDLL('crypt32',use_last_error=True)
    call = crypt.CryptUnprotectData if unlock else crypt.CryptProtectData
    call.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,
                   ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    call.restype=wintypes.BOOL
    if not call(ctypes.byref(source),None,None,None,None,1,ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(),'Windows 未能处理当前账户的机器人凭据')
    try:
        return ctypes.string_at(result.ptr,result.size)
    finally:
        kernel = ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes=[ctypes.c_void_p]
        kernel.LocalFree.restype=ctypes.c_void_p
        kernel.LocalFree(result.ptr)


def credential_path(config, app):
    if not APP.fullmatch(app):
        raise ValueError('无效的飞书应用 ID')
    return config.parent/'.specialist-credentials'/(app+'.bin')


def write_atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as temp:
        pending=Path(temp.name)
        temp.write(data)
        temp.flush()
        os.fsync(temp.fileno())
    try:
        os.replace(pending,path)
    finally:
        pending.unlink(missing_ok=True)


def prompt_secret():
    import sys
    if not sys.stdin.isatty():
        raise ValueError('请在交互终端中输入密钥，或使用 --env-only')
    return getpass.getpass('输入此专业应用的 App Secret（输入时不显示）：').strip()


def add(config, name, agent, app, owner, group, env_name=None, *, env_only=False):
    original=config.read_bytes()
    cfg=json.loads(original.decode('utf-8-sig'))
    env_name=env_name or f'FEISHU_AGENT_{agent}_SECRET'
    if not (name.strip() and name!='steward' and agent>0 and APP.fullmatch(app)
            and re.fullmatch(r'ou_[A-Za-z0-9]+',owner) and re.fullmatch(r'oc_[A-Za-z0-9]+',group)
            and SECRET_ENV.fullmatch(env_name)):
        raise ValueError('请核对角色名、本地角色 ID、飞书应用 ID、老板 open_id、群 chat_id 和密钥变量名')
    identities=[cfg['steward'],*cfg['roles'].values()]
    if name in cfg['roles'] or any(r['agent_id']==agent or r['app_id']==app or r['secret_env']==env_name for r in identities):
        raise ValueError('该角色、应用、密钥变量或本地角色 ID 已被使用')
    if not env_only:
        destination=credential_path(config,app)
        if destination.exists():
            raise ValueError('此应用已有凭据文件，请检查原配置')
        secret=prompt_secret()
        if not secret:
            raise ValueError('App Secret 为空')
        encrypted=dpapi(secret.encode('utf-8'),unlock=False)
    if original!=config.read_bytes():
        raise ValueError('配置已变更，请重新打开后再添加')
    cfg['roles'][name]={'agent_id':agent,'app_id':app,'secret_env':env_name,'chat_id':group,'owner_open_id':owner}
    if not env_only:
        write_atomic(destination,encrypted)
    write_atomic(config,(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))


def load_secrets(config,cfg):
    for role in cfg['roles'].values():
        name=role['secret_env']
        if os.environ.get(name):
            continue
        filename=credential_path(config,role['app_id'])
        if not SECRET_ENV.fullmatch(name) or not filename.is_file():
            raise ValueError(f'请为应用 {role["app_id"]} 配置专用密钥变量 {name}')
        os.environ[name]=dpapi(filename.read_bytes(),unlock=True).decode('utf-8')


def main():
    parser=argparse.ArgumentParser(description='在本地登记一个专业机器人。保存后由现有网关在下次启动时加载。')
    parser.add_argument('--config',default=os.getenv('FEISHU_CONFIG'))
    parser.add_argument('--role')
    parser.add_argument('--agent',type=int)
    parser.add_argument('--app-id')
    parser.add_argument('--owner-open-id')
    parser.add_argument('--group')
    parser.add_argument('--secret-env')
    parser.add_argument('--env-only',action='store_true',help='由运行网关的外部环境提供密钥')
    args=parser.parse_args()
    try:
        config=Path(args.config or input('现有飞书配置文件路径：')).expanduser().resolve(strict=True)
        add(config,args.role or input('角色名：').strip(),args.agent or int(input('本地数字角色 ID：')),
            args.app_id or input('此角色的飞书 App ID：').strip(),
            args.owner_open_id or input('老板在该应用下的 open_id：').strip(),
            args.group or input('授权工作群的 chat_id：').strip(),args.secret_env,env_only=args.env_only)
    except (OSError,KeyError,ValueError) as error:
        parser.exit(1,f'登记未完成：{error}\n')
    print('已保存本地角色配置，将在网关下次启动时生效。')


if __name__=='__main__':
    main()
