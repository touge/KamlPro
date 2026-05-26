import argparse
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 最优先加载 .env 文件中的密钥，确保 yaml_config_loader 的 ${ENV_VAR} 展开能读到正确值
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

import uvicorn
from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.platform_log_filter import setup_platform_log_filter
from src.core.service_controller import service_controller


def main():
    parser = argparse.ArgumentParser(description="Kalm - AI Gateway Controller")
    parser.add_argument("--host", type=str, default=None, help="Host to listen on")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    args, _ = parser.parse_known_args()

    setup_platform_log_filter()

    # Kalm 启动时自动拉起 auto_start: true 的服务（常驻，不受引用计数回收）
    service_controller.start_auto_services()

    # 命令行参数优先，其次为配置文件，最后使用默认值
    host = args.host or yaml_config_loader.get("api_server.host", "0.0.0.0")
    port = args.port or yaml_config_loader.get("api_server.port", 7000)

    uvicorn.run(
        "src.api.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
