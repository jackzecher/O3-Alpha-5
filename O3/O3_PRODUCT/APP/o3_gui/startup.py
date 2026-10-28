"""Welcome screen for the explicit review launcher only, never the managed launcher."""
from PySide6.QtCore import Qt, QObject, QRectF
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QApplication
from .appearance import normalize_appearance, mix
from .presentation import application_icon, theme_tokens, stylesheet_for, native_palette
from .ui import text, btn, rule


class WelcomeScreen(QDialog):
    def __init__(self, appearance=None, parent=None, preview=False):
        super().__init__(parent)
        self.appearance = normalize_appearance(appearance)
        self.colors = theme_tokens(self.appearance['theme'],self.appearance)
        self.setWindowTitle('Welcome to O3' if preview else 'Opening O3')
        self.setWindowIcon(application_icon())
        self.setWindowFlags(Qt.Dialog if preview else Qt.SplashScreen)
        self.setStyleSheet(stylesheet_for(scale=self.appearance['text_scale'],theme=self.appearance['theme'],appearance=self.appearance))
        self.setPalette(native_palette(self.appearance['theme'],self.appearance))
        self.resize(710,470); self.setMinimumSize(440,360)
        layout = QVBoxLayout(self); layout.setContentsMargins(44,38,44,28); layout.setSpacing(16)
        logo = QLabel(); logo.setPixmap(application_icon().pixmap(84,84)); logo.setAccessibleName('O3'); logo.setStyleSheet('background:transparent;')
        layout.addWidget(logo,0,Qt.AlignLeft); layout.addSpacing(8)
        layout.addWidget(text('Your work, connected.','title',True))
        layout.addWidget(text('A clearer view of your day.','muted',True)); layout.addStretch(1)
        layout.addWidget(rule()); self.status = text('Welcome screen preview' if preview else 'Opening your workspace…','body',True)
        self.status.setAccessibleName(self.status.text()); layout.addWidget(self.status)
        if preview: layout.addWidget(btn('Close preview',self.accept,kind='secondary'),0,Qt.AlignRight)
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            available=screen.availableGeometry();self.resize(min(self.width(),available.width()-40),min(self.height(),available.height()-40))
            self.move(available.center()-self.rect().center())

    def paintEvent(self,event):
        super().paintEvent(event)
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(mix(self.colors['canvas'],self.colors['accent'],.12)),1));painter.setBrush(Qt.NoBrush)
        for radius in (78,125,172):
            painter.drawEllipse(QRectF(self.width()-90-radius,80-radius,2*radius,2*radius))


class ReviewStartup(QObject):
    """One real handoff, no minimum dwell or simulated completion percentage."""
    def __init__(self,window,splash):
        super().__init__(window);self.window=window;self.splash=splash;self.finished=False;self.succeeded=None
        window.startup_finished.connect(self.finish)
        splash.rejected.connect(self.cancel)
        if window.initialized:self.finish(True)

    def finish(self,success):
        if self.finished:return
        self.finished=True;self.succeeded=bool(success)
        self.window.show();self.splash.accept()

    def cancel(self):
        # Show the actual window before requesting close so its worker can stop
        # through the normal close path, without orphaning a running QThread.
        if self.finished:return
        self.finish(False);self.window.close()
