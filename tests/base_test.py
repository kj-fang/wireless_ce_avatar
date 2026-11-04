"""Shared fixture for every blueprint test module.

Why this exists
---------------
Every route module does ``from configs.global_configs import app_config`` and
therefore binds a reference to the real ``GlobalConfig`` singleton AT IMPORT
TIME. Patching only ``configs.global_configs.app_config`` is not enough: a
route module that was imported earlier (the VS Code test runner imports test
modules - and therefore blueprints - before any setUpClass runs) would keep
pointing at the real object. ``BaseTestCase.setUpClass`` therefore swaps the
singleton for a MagicMock and repoints EVERY loaded module whose
``app_config`` attribute *is* the real object (identity match) to that mock.

Branch-specific: ``app_config.silent_mode`` exists on this branch and gates the
explorer / TextAnalysisTool launches. The mock defaults it to ``False`` so the
"GUI would open" path is the deterministic default; tests still patch
``subprocess`` so nothing is ever launched.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Same console fix as tests/__init__.py - repeated here because
# ``unittest discover -s tests`` imports test modules as top-level modules
# without executing the package __init__.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from flask import Blueprint, Flask

import configs.global_configs as global_configs
# Import every blueprint up front so each route module binds the REAL
# app_config first; setUpClass then rebinds them all deterministically,
# regardless of which test module the runner happened to import first.
import blueprints  # noqa: F401
from models.models import CaseContext

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
# Fake locations only - tests never create or write to these.
TEST_PROJECT_ROOT = os.path.join(TESTS_DIR, "_fake_project_root")
TEST_AVATARFILES_DIR = os.path.join(TESTS_DIR, "_fake_avatarfiles")
TEST_DRIVER_DIR = os.path.join(TESTS_DIR, "_fake_driver")
TEST_DOWNLOAD_DIR = os.path.join(TEST_AVATARFILES_DIR, "downloads")

TEST_CASE_NBR = "00123456"


class BaseTestCase(unittest.TestCase):
    """Fresh Flask app + test client per test, with a mocked ``app_config``.

    Subclasses set:
      * ``blueprint``           - the Blueprint under test (registered in setUp)
      * ``main_stub_endpoints`` - endpoints of a lightweight stub ``main``
        blueprint, for routes that ``redirect(url_for('main.<endpoint>'))``.
    """

    blueprint = None
    main_stub_endpoints = ()

    # ------------------------------------------------------------------ class
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._patchers = []
        cls.real_app_config = global_configs.app_config
        cls.mock_app_config = MagicMock(name="app_config")
        cls._configure_app_config(cls.mock_app_config)

        cls._start_patch(patch("configs.global_configs.app_config", cls.mock_app_config))
        cls.rebound_modules = []
        for name, module in list(sys.modules.items()):
            if module is None or module is global_configs:
                continue
            try:
                bound = vars(module).get("app_config")
            except TypeError:
                continue
            if bound is cls.real_app_config:
                cls._start_patch(patch.object(module, "app_config", cls.mock_app_config))
                cls.rebound_modules.append(name)

    @classmethod
    def tearDownClass(cls):
        for patcher in reversed(cls._patchers):
            patcher.stop()
        cls._patchers = []
        super().tearDownClass()

    @classmethod
    def _start_patch(cls, patcher):
        started = patcher.start()
        cls._patchers.append(patcher)
        return started

    @staticmethod
    def _configure_app_config(mock_cfg):
        """(Re)apply the deterministic app_config defaults."""
        mock_cfg.reset_mock()
        mock_cfg.socketio = MagicMock(name="socketio")
        mock_cfg.llm_helper = MagicMock(name="llm_helper")
        mock_cfg.key = MagicMock(name="key")
        mock_cfg.driver_manager = MagicMock(name="driver_manager")
        mock_cfg.log_chatbot_agent = None
        mock_cfg.nw_analysis_agent = None
        mock_cfg.bt_chatbot_agent = None
        mock_cfg.silent_mode = False
        mock_cfg.project_root = TEST_PROJECT_ROOT
        mock_cfg.avatarfiles_dir = TEST_AVATARFILES_DIR
        mock_cfg.driver_dir = TEST_DRIVER_DIR
        mock_cfg.prompt_dir = os.path.join(TEST_PROJECT_ROOT, "prompts")
        mock_cfg.sendto_token = "unit-test-token"
        mock_cfg.last_analyzed_log_path = None
        mock_cfg.download_results = {}
        mock_cfg.etl_exclude_keywords = ["PldrComplete", "Autologger", "Dump_failed", "AutoDump"]

    # ------------------------------------------------------------------- test
    def setUp(self):
        super().setUp()
        # Per-test reset so call history / attribute tweaks never leak.
        self._configure_app_config(self.mock_app_config)
        self.app_config = self.mock_app_config

        # CaseContext.to_session() writes a sidecar JSON into
        # case_download_dir. Keep tests hermetic: return the full dict, which
        # is exactly what to_session() does when no download dir is set.
        to_session_patcher = patch.object(
            CaseContext, "to_session", autospec=True,
            side_effect=lambda ctx: ctx.to_dict(),
        )
        to_session_patcher.start()
        self.addCleanup(to_session_patcher.stop)

        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="unit-test-secret",
            WTF_CSRF_ENABLED=False,
        )
        if self.blueprint is not None:
            self.app.register_blueprint(self.blueprint)
        if self.main_stub_endpoints:
            self.register_main_stub(*self.main_stub_endpoints)

        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()
        super().tearDown()

    # ---------------------------------------------------------------- helpers
    def register_main_stub(self, *endpoints):
        """Register a stub 'main' blueprint exposing only ``endpoints``.

        Each stub endpoint lives at ``/__main_stub__/<endpoint>`` so redirect
        Location headers are easy to assert on.
        """
        stub = Blueprint("main", __name__)
        for endpoint in endpoints:
            stub.add_url_rule(
                f"/__main_stub__/{endpoint}",
                endpoint=endpoint,
                view_func=lambda endpoint=endpoint: f"stub:{endpoint}",
            )
        self.app.register_blueprint(stub)
        return stub

    @staticmethod
    def main_stub_path(endpoint):
        return f"/__main_stub__/{endpoint}"

    @staticmethod
    def create_mock_case_context(**overrides):
        """A session-ready dict carrying exactly the real CaseContext fields.

        Built through ``CaseContext(...).to_dict()`` so a renamed/removed
        dataclass field fails loudly here instead of silently in a route.
        """
        values = dict(
            case_nbr=TEST_CASE_NBR,
            id="a0B000000TESTID",
            backend_id="",
            subject="Wi-Fi disconnects after resume",
            description="Wi-Fi drops every time the laptop resumes from Modern Standby.",
            env_detail={
                "Computer Model": "TestModel X1",
                "Steps to reproduce": "1. Enter Modern Standby 2. Resume",
            },
            subcategory="Wireless - WiFi",
            comments=[],
            attachment_info={},
            attachment_list=[],
            case_download_dir=os.path.join(TEST_DOWNLOAD_DIR, TEST_CASE_NBR),
            ips_pdf_path=None,
            error_message=None,
            wifi_or_bt="wifi",
            files_coexist=False,
        )
        values.update(overrides)
        return CaseContext(**values).to_dict()

    def seed_session(self, **extra):
        """Seed the cookie session with a mock case_context plus ``extra`` keys."""
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context()
            for key, value in extra.items():
                sess[key] = value

    def get_session(self):
        with self.client.session_transaction() as sess:
            return dict(sess)
