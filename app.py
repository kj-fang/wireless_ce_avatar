from flask import Flask
from flask_socketio import SocketIO, emit
from threading import Thread, Event


from services.driver_manage_service import DriverManager
from configs.set_up_app import set_up
from configs.global_configs import app_config

#from blueprints.main import main_bp
#from blueprints.attachment import attachment_bp
#from blueprints.log_analysis import log_bp
from blueprints import automation_bp, main_bp, llm_bp, download_bp, analysis_etl_bp, bsod_bp, log_parser_bp # , attachment_bp, log_bp, 
import blueprints.download.download_routes


def create_app():
    app = Flask(__name__)
    app.config['JSON_SORT_KEYS'] = False
    app.secret_key = 'autoparselog2025'
    socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

    # Register blueprints

    app.register_blueprint(main_bp)
    app.register_blueprint(analysis_etl_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(llm_bp)
    app.register_blueprint(automation_bp)
    app.register_blueprint(bsod_bp)
    app.register_blueprint(log_parser_bp)

    # Register socketio
    blueprints.download.download_routes.register_socketio_handlers(socketio)
    blueprints.log_parser.log_parser_routes.register_socketio_handlers(socketio)
    blueprints.automation.automation_routes.register_socketio_handlers(socketio)

    print("📋 Registered routes:")
    for rule in app.url_map.iter_rules():
        print(f"  {rule.endpoint}: {rule.rule}")

    return app, socketio


if __name__ == "__main__":
    app, socketio = create_app()
    set_up(socketio)
    app_config.set_driver_manager(DriverManager(app_config.avatarfiles_dir))
    
    app_config.driver_manager.run_driver(socketio, app)
    
