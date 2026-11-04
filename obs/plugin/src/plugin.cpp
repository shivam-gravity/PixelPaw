// PixelPaw for OBS Studio (30+): a native plugin that gives PixelPaw one real OBS dock, "PixelPaw"
// (dockable, tabbable and listed under the Docks menu, like YouTube Live Chat, Scenes and Controls),
// starts PixelPaw with OBS and adds PixelPaw actions to the Tools menu.
//
// The dock is PixelPaw's universal control: live status, actions, activity, drafts and settings in one window.
// It only creates its browser while visible, and only once PixelPaw answers on /healthz,
// so the plugin costs nothing while PixelPaw is not running.

#include <obs-module.h>
#include <obs-frontend-api.h>
#include <util/platform.h>
#include <browser-panel.hpp>

#include <QAction>
#include <QApplication>
#include <QDesktopServices>
#include <QDir>
#include <QDockWidget>
#include <QFileInfo>
#include <QLabel>
#include <QMainWindow>
#include <QMenu>
#include <QNetworkAccessManager>
#include <QNetworkProxy>
#include <QNetworkReply>
#include <QPointer>
#include <QProcess>
#include <QPushButton>
#include <QTimer>
#include <QUrl>
#include <QUrlQuery>
#include <QVBoxLayout>

#include <string>
#include <vector>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("pixelpaw", "en-US")

#define LOG(level, fmt, ...) blog(level, "[PixelPaw] " fmt, ##__VA_ARGS__)

MODULE_EXPORT const char *obs_module_description(void)
{
	return "PixelPaw: one dock to run your YouTube bot from OBS, plus the alerts overlay.";
}

namespace {

const char *OVERLAY_SOURCE = "PixelPaw Overlay";
const char *LAYOUT_FLAG = "layout-v2.done"; // v2: the single PixelPaw dock

struct Config {
	QString url = "http://127.0.0.1:8765";
	QString folder;
	bool autostart = true;
};
Config cfg;

QCef *cef = nullptr;
bool cefReady = false;
int cefWaits = 0; // half-second checks while OBS's browser is still starting
bool serverUp = false;
QPointer<QNetworkAccessManager> net;
QPointer<QTimer> poller;

// ---- config --------------------------------------------------------------------------------
void load_config()
{
	char *path = obs_module_config_path("config.json");
	obs_data_t *data = path ? obs_data_create_from_json_file_safe(path, "bak") : nullptr;
	if (data) {
		obs_data_set_default_string(data, "url", "http://127.0.0.1:8765");
		obs_data_set_default_bool(data, "autostart", true);
		cfg.url = QString::fromUtf8(obs_data_get_string(data, "url"));
		cfg.folder = QString::fromUtf8(obs_data_get_string(data, "folder"));
		cfg.autostart = obs_data_get_bool(data, "autostart");
		obs_data_release(data);
	}
	while (cfg.url.endsWith('/'))
		cfg.url.chop(1);
	LOG(LOG_INFO, "url=%s folder=%s autostart=%d", cfg.url.toUtf8().constData(),
	    cfg.folder.isEmpty() ? "(not set)" : cfg.folder.toUtf8().constData(), (int)cfg.autostart);
	bfree(path);
}

bool layout_done()
{
	char *path = obs_module_config_path(LAYOUT_FLAG);
	bool done = path && os_file_exists(path);
	bfree(path);
	return done;
}

void mark_layout_done()
{
	char *dir = obs_module_config_path("");
	if (dir)
		os_mkdirs(dir);
	bfree(dir);
	char *path = obs_module_config_path(LAYOUT_FLAG);
	if (path)
		os_quick_write_utf8_file(path, "1", 1, false);
	bfree(path);
}

// ---- starting PixelPaw ---------------------------------------------------------------------
bool can_launch()
{
	return !cfg.folder.isEmpty() && QFileInfo::exists(QDir(cfg.folder).filePath("PixelPaw.pyw"));
}

void launch_pixelpaw(bool open_dashboard)
{
	if (!can_launch()) {
		LOG(LOG_WARNING, "PixelPaw folder not set or missing PixelPaw.pyw (%s)", cfg.folder.toUtf8().constData());
		return;
	}
	QDir dir(cfg.folder);
	QStringList extra;
	if (!open_dashboard)
		extra << "--no-browser";
	bool ok;
#ifdef _WIN32
	QString pythonw = dir.filePath(".venv/Scripts/pythonw.exe");
	if (QFileInfo::exists(pythonw))
		ok = QProcess::startDetached(pythonw, QStringList{dir.filePath("PixelPaw.pyw")} + extra, cfg.folder);
	else // first run: PixelPaw.cmd installs itself in a visible window, once
		ok = QProcess::startDetached("cmd.exe",
					     QStringList{"/c", "start", "", "/D", QDir::toNativeSeparators(cfg.folder),
							 QDir::toNativeSeparators(dir.filePath("PixelPaw.cmd"))} +
						     extra,
					     cfg.folder);
#else
	ok = QProcess::startDetached("/bin/sh", QStringList{dir.filePath("PixelPaw.command")} + extra, cfg.folder);
#endif
	LOG(ok ? LOG_INFO : LOG_WARNING, "%s PixelPaw", ok ? "started" : "could not start");
}

// ---- docks ---------------------------------------------------------------------------------
struct DockSpec {
	const char *id;
	const char *titleKey;
	const char *view; // ?dock=<view>
};

// One window for everything. (Kept as a list so a dock could be split out again later.)
const DockSpec DOCKS[] = {
	{"pixelpaw", "Dock.Main", "panel"},
};
const char *MAIN_DOCK = "pixelpaw";

QString theme_query()
{
	QColor bg = QApplication::palette().color(QPalette::Window);
	QColor fg = QApplication::palette().color(QPalette::WindowText);
	return QString("theme=%1&bg=%2&fg=%3")
		.arg(bg.lightness() < 128 ? "dark" : "light", bg.name().mid(1), fg.name().mid(1));
}

class Panel : public QWidget {
public:
	Panel(const DockSpec &spec) : QWidget(nullptr), spec(spec)
	{
		// Small minimum: the dock must never make OBS's main window taller or wider than the screen.
		setMinimumSize(80, 40);
		auto *layout = new QVBoxLayout(this);
		layout->setContentsMargins(0, 0, 0, 0);
		placeholder = new QWidget(this);
		placeholder->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Ignored);
		auto *pl = new QVBoxLayout(placeholder);
		pl->addStretch();
		status = new QLabel(placeholder);
		status->setAlignment(Qt::AlignCenter);
		status->setWordWrap(true);
		pl->addWidget(status);
		startButton = new QPushButton(obs_module_text("StartPixelPaw"), placeholder);
		startButton->setVisible(false);
		QObject::connect(startButton, &QPushButton::clicked, [this]() {
			launch_pixelpaw(false);
			startButton->setEnabled(false);
			status->setText(obs_module_text("Starting"));
			QTimer::singleShot(15000, startButton, [this]() { startButton->setEnabled(true); });
		});
		pl->addWidget(startButton, 0, Qt::AlignHCenter);
		pl->addStretch();
		layout->addWidget(placeholder);
		status->setText(obs_module_text("Starting"));
	}

	QString url() const
	{
		return cfg.url + "/?" + theme_query() + "&dock=" + spec.view;
	}

	void serverChanged(bool up, bool longWait)
	{
		if (browser)
			return;
		if (up) {
			maybeCreateBrowser();
			return;
		}
		if (longWait) {
			status->setText(obs_module_text(can_launch() ? "NotRunning" : "NotRunningNoFolder"));
			startButton->setVisible(can_launch());
		}
	}

	void maybeCreateBrowser()
	{
		if (browser || !serverUp || !isVisible())
			return;
		if (!cef) {
			cef = obs_browser_init_panel();
			if (!cef) {
				status->setText(obs_module_text("NoBrowser"));
				return;
			}
		}
		// Never block OBS's UI thread waiting for Chromium (if it fails to start, the wait never ends):
		// check again shortly instead, like OBS's own docks do with a background wait.
		if (!cefReady) {
			if (cef->init_browser() || cef->initialized()) {
				cefReady = true;
			} else if (++cefWaits > 60) {
				status->setText(obs_module_text("NoBrowser"));
				if (cefWaits == 61)
					LOG(LOG_WARNING, "OBS's browser (CEF) did not start; the PixelPaw dock stays empty");
				return;
			} else {
				QTimer::singleShot(500, this, [this]() { maybeCreateBrowser(); });
				return;
			}
		}
		browser = cef->create_widget(this, url().toStdString(), nullptr);
		if (!browser) {
			status->setText(obs_module_text("NoBrowser"));
			return;
		}
		placeholder->hide();
		layout()->addWidget(browser);
		LOG(LOG_INFO, "dock %s loaded", spec.id);
	}

	void reload()
	{
		if (browser)
			browser->setURL(url().toStdString());
	}

	void shutdown()
	{
		if (browser) {
			browser->closeBrowser();
			delete browser.data();
		}
	}

	const DockSpec &spec;

protected:
	void showEvent(QShowEvent *event) override
	{
		QWidget::showEvent(event);
		maybeCreateBrowser();
	}

private:
	QWidget *placeholder = nullptr;
	QLabel *status = nullptr;
	QPushButton *startButton = nullptr;
	QPointer<QCefWidget> browser;
};

std::vector<QPointer<Panel>> panels;

QMainWindow *main_window()
{
	return static_cast<QMainWindow *>(obs_frontend_get_main_window());
}

QDockWidget *find_dock(const char *name)
{
	QMainWindow *main = main_window();
	return main ? main->findChild<QDockWidget *>(name) : nullptr;
}

void show_dock(const char *id)
{
	if (QDockWidget *dock = find_dock(id)) {
		dock->setVisible(true);
		dock->raise();
	}
}

// "Enabled" the way the Docks menu sees it: true for a dock that is a background tab, false for a closed one.
bool enabled(QDockWidget *d)
{
	return d && d->toggleViewAction()->isChecked();
}

// First run only: give the PixelPaw dock its own space in the bottom row, right after OBS's
// Controls, so it sits beside Scenes, Sources and the mixer without covering YouTube's chat.
// Afterwards OBS saves and restores wherever the user moves it.
void first_run_layout()
{
	if (layout_done())
		return;
	QMainWindow *main = main_window();
	QDockWidget *dock = find_dock(MAIN_DOCK);
	if (!main || !dock)
		return;

	dock->setFloating(false);
	dock->setVisible(true);
	// A new split starts at its minimum width: ask for a real width while Qt places it.
	dock->widget()->setMinimumWidth(320);
	QDockWidget *obsControls = main->findChild<QDockWidget *>("controlsDock");
	bool besideControls = enabled(obsControls) && !obsControls->isFloating();
	if (besideControls)
		main->splitDockWidget(obsControls, dock, Qt::Horizontal);
	else
		main->addDockWidget(Qt::RightDockWidgetArea, dock);
	QTimer::singleShot(1500, dock, [dock]() { dock->widget()->setMinimumWidth(80); }); // user may shrink it again

	mark_layout_done();
	LOG(LOG_INFO, "first-run dock layout applied (PixelPaw %s)", besideControls ? "beside OBS Controls" : "on the right");
}

// Written to the OBS log (Help → Log Files) so a layout can be checked without a screenshot.
void log_docks()
{
	QMainWindow *main = main_window();
	if (!main)
		return;
	for (QDockWidget *w : main->findChildren<QDockWidget *>()) {
		if (!enabled(w))
			continue;
		QStringList tabs;
		for (QDockWidget *o : main->tabifiedDockWidgets(w))
			tabs << o->windowTitle();
		const char *area = "?";
		switch (main->dockWidgetArea(w)) {
		case Qt::LeftDockWidgetArea: area = "left"; break;
		case Qt::RightDockWidgetArea: area = "right"; break;
		case Qt::TopDockWidgetArea: area = "top"; break;
		case Qt::BottomDockWidgetArea: area = "bottom"; break;
		default: break;
		}
		if (w->isFloating())
			area = "floating";
		QString extra = tabs.isEmpty() ? QString() : " tabbed with: " + tabs.join(", ");
		LOG(LOG_INFO, "dock %s \"%s\": %s, %s, %dx%d%s", w->objectName().toUtf8().constData(),
		    w->windowTitle().toUtf8().constData(), w->isVisible() ? "showing" : "background tab", area, w->width(),
		    w->height(), extra.toUtf8().constData());
	}
}

// Lay out once the main window is on screen with its real size: Qt doesn't size docks inside a
// minimised window, and OBS may start minimised (or in the tray).
void schedule_layout()
{
	QTimer::singleShot(1000, main_window(), []() {
		QMainWindow *main = main_window();
		if (!main)
			return;
		if (!layout_done() && (main->isMinimized() || !main->isVisible())) {
			schedule_layout();
			return;
		}
		first_run_layout();
		QTimer::singleShot(2000, main, []() {
			log_docks();
			// Testing aid: PIXELPAW_OBS_SNAPSHOT=1 saves a picture of the OBS window's layout
			// (Qt-drawn parts only: the preview and browser contents stay blank).
			if (qEnvironmentVariableIsSet("PIXELPAW_OBS_SNAPSHOT")) {
				char *path = obs_module_config_path("layout.png");
				if (path && main_window()->grab().save(QString::fromUtf8(path)))
					LOG(LOG_INFO, "layout snapshot saved: %s", path);
				bfree(path);
			}
		});
	});
}

// ---- server watch --------------------------------------------------------------------------
int failedChecks = 0;

void set_server(bool up)
{
	bool changed = up != serverUp;
	serverUp = up;
	failedChecks = up ? 0 : failedChecks + 1;
	bool longWait = failedChecks == 8; // ~16 s after start
	if (!changed && !longWait)
		return;
	if (changed)
		LOG(LOG_INFO, "PixelPaw %s", up ? "is running" : "is not running");
	for (auto &p : panels)
		if (p)
			p->serverChanged(up, longWait || (!up && failedChecks > 8));
}

void check_server()
{
	if (!net)
		return;
	QNetworkRequest req(QUrl(cfg.url + "/healthz"));
	req.setTransferTimeout(1500);
	QNetworkReply *reply = net->get(req);
	QObject::connect(reply, &QNetworkReply::finished, reply, [reply]() {
		bool ok = reply->error() == QNetworkReply::NoError && reply->readAll().contains("pixelpaw");
		reply->deleteLater();
		set_server(ok);
		if (poller)
			poller->setInterval(ok ? 10000 : 2000);
	});
}

// ---- overlay (same as obs/pixelpaw.lua) ----------------------------------------------------
obs_data_t *overlay_settings()
{
	obs_video_info ovi = {};
	int w = 1920, h = 1080;
	if (obs_get_video_info(&ovi)) {
		w = (int)ovi.base_width;
		h = (int)ovi.base_height;
	}
	obs_data_t *s = obs_data_create();
	obs_data_set_bool(s, "is_local_file", false);
	obs_data_set_string(s, "url", (cfg.url + "/overlay").toUtf8().constData());
	obs_data_set_int(s, "width", w);
	obs_data_set_int(s, "height", h);
	obs_data_set_int(s, "fps", 30);
	obs_data_set_bool(s, "shutdown", false);
	obs_data_set_bool(s, "restart_when_active", false);
	obs_data_set_string(s, "css", "body { background-color: rgba(0, 0, 0, 0); margin: 0; overflow: hidden; }");
	return s;
}

void add_overlay()
{
	obs_source_t *scene_source = obs_frontend_get_current_scene();
	if (!scene_source) {
		LOG(LOG_WARNING, "no current scene");
		return;
	}
	obs_scene_t *scene = obs_scene_from_source(scene_source);
	obs_data_t *settings = overlay_settings();
	obs_source_t *source = obs_get_source_by_name(OVERLAY_SOURCE);
	if (!source)
		source = obs_source_create("browser_source", OVERLAY_SOURCE, settings, nullptr);
	else
		obs_source_update(source, settings);
	if (source) {
		if (!obs_scene_find_source(scene, OVERLAY_SOURCE))
			obs_scene_add(scene, source);
		LOG(LOG_INFO, "overlay added to the current scene");
		obs_source_release(source);
	} else {
		LOG(LOG_WARNING, "could not create a Browser Source (is obs-browser installed?)");
	}
	obs_data_release(settings);
	obs_source_release(scene_source);
}

// ---- menu ----------------------------------------------------------------------------------
void build_tools_menu()
{
	auto *action = static_cast<QAction *>(obs_frontend_add_tools_menu_qaction(obs_module_text("Menu.PixelPaw")));
	if (!action)
		return;
	auto *menu = new QMenu();
	QObject::connect(menu->addAction(obs_module_text("Menu.Show")), &QAction::triggered, []() { show_dock(MAIN_DOCK); });
	QObject::connect(menu->addAction(obs_module_text("Menu.Browser")), &QAction::triggered,
			 []() { QDesktopServices::openUrl(QUrl(cfg.url + "/")); });
	menu->addSeparator();
	QObject::connect(menu->addAction(obs_module_text("Menu.AddOverlay")), &QAction::triggered, []() { add_overlay(); });
	QObject::connect(menu->addAction(obs_module_text("Menu.Reload")), &QAction::triggered, []() {
		for (auto &p : panels)
			if (p)
				p->reload();
	});
	QAction *start = menu->addAction(obs_module_text("StartPixelPaw"));
	QObject::connect(start, &QAction::triggered, []() { launch_pixelpaw(false); });
	QObject::connect(menu, &QMenu::aboutToShow, [start]() { start->setEnabled(can_launch() && !serverUp); });
	action->setMenu(menu);
}

void on_frontend_event(enum obs_frontend_event event, void *)
{
	if (event == OBS_FRONTEND_EVENT_FINISHED_LOADING) {
		schedule_layout();
		net = new QNetworkAccessManager(main_window());
		net->setProxy(QNetworkProxy::NoProxy);
		poller = new QTimer(main_window());
		QObject::connect(poller, &QTimer::timeout, []() { check_server(); });
		poller->start(2000);
		check_server();
	} else if (event == OBS_FRONTEND_EVENT_EXIT) {
		if (poller)
			poller->stop();
		for (auto &p : panels)
			if (p)
				p->shutdown();
	}
}

} // namespace

bool obs_module_load(void)
{
	load_config();
	if (cfg.autostart)
		launch_pixelpaw(false); // PixelPaw is single-instance: if it already runs, this does nothing

	for (const auto &spec : DOCKS) {
		auto *panel = new Panel(spec);
		if (!obs_frontend_add_dock_by_id(spec.id, obs_module_text(spec.titleKey), panel)) {
			LOG(LOG_WARNING, "could not add dock %s", spec.id);
			delete panel;
			continue;
		}
		panels.emplace_back(panel);
	}
	build_tools_menu();
	obs_frontend_add_event_callback(on_frontend_event, nullptr);
	LOG(LOG_INFO, "loaded (%zu docks)", panels.size());
	return true;
}

void obs_module_unload(void)
{
	obs_frontend_remove_event_callback(on_frontend_event, nullptr);
}
