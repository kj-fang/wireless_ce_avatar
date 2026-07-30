    class LogController {
        constructor(strategy) { this.strategy = strategy; }
        browseLog(...args) { return this.strategy.browseLog(...args); }
        copyLogPath(...args) { return this.strategy.copyLogPath(...args); }
        setLog(...args) { return this.strategy.setLog(...args); }
    }

    class ChatController {
        constructor(strategy) { this.strategy = strategy; }
        sendMessage(...args) { return this.strategy.sendMessage(...args); }
    }

    class ReportRenderer {
        constructor(strategy) { this.strategy = strategy; }
        appendReport(...args) { return this.strategy.appendReport(...args); }
        appendIncidentTag(...args) { return this.strategy.appendIncidentTag(...args); }
    }

    class AutoRunController {
        constructor(strategy) { this.strategy = strategy; }
        tryAutoAnalyzeOnLoad(...args) {
            return this.strategy.tryAutoAnalyzeOnLoad(...args);
        }
    }

    const chatRuntimeProfile = window.CHATBOT || {};
    const chatRuntimeStrategy =
        typeof window.createChatRuntimeStrategy === 'function'
            ? window.createChatRuntimeStrategy(chatRuntimeProfile)
            : null;

    window.logController = new LogController(chatRuntimeStrategy);
    window.chatController = new ChatController(chatRuntimeStrategy);
    window.reportRenderer = new ReportRenderer(chatRuntimeStrategy);
    window.autoRunController = new AutoRunController(chatRuntimeStrategy);

    window.browseLog = (...args) => window.logController.browseLog(...args);
    window.copyLogPath = (...args) => window.logController.copyLogPath(...args);
    window.setLog = (...args) => window.logController.setLog(...args);
    window.sendMessage = (...args) => window.chatController.sendMessage(...args);
    window.appendReport = (...args) => window.reportRenderer.appendReport(...args);
    window.appendIncidentTag =
        (...args) => window.reportRenderer.appendIncidentTag(...args);
    window.tryAutoAnalyzeOnLoad =
        (...args) => window.autoRunController.tryAutoAnalyzeOnLoad(...args);
