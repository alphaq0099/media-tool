"""Two history areas: saved jobs and tabbed file/step details."""
from PySide6.QtCore import QObject,QEvent,Qt,QTimer
from PySide6.QtWidgets import QWidget,QVBoxLayout,QLabel,QTabWidget,QHeaderView


class HistoryLayout(QObject):
    def __init__(self,queue,page,layout):
        super().__init__(page)
        self.queue=queue
        self.page=page
        self.child_key=None
        self.scroll_timer=QTimer(self)
        self.scroll_timer.setSingleShot(True)
        self.scroll_timer.timeout.connect(self.scroll_to_selection)
        splitter=queue.jobs_splitter
        self.jobs=QWidget()
        jobs_layout=QVBoxLayout(self.jobs)
        jobs_layout.setContentsMargins(0,0,0,0)
        jobs_layout.setSpacing(8)
        jobs_layout.addWidget(QLabel('Saved jobs'))
        jobs_layout.addWidget(queue.jobs_table,1)
        self.details=QWidget()
        details_layout=QVBoxLayout(self.details)
        details_layout.setContentsMargins(0,0,0,0)
        details_layout.setSpacing(8)
        self.tabs=QTabWidget()
        self.tabs.setAccessibleName('History file details')
        self.tabs.currentChanged.connect(lambda:self.scroll_timer.start(0))
        self.tabs.addTab(queue.items_table,'Files')
        self.tabs.addTab(queue.child_table,'Step files')
        self.tabs.setTabVisible(1,False)
        queue.items_table.show()
        details_layout.addWidget(self.tabs,1)
        layout.removeWidget(queue.detail)
        queue.detail.setMaximumHeight(56)
        details_layout.addWidget(queue.detail)
        splitter.addWidget(self.jobs)
        splitter.addWidget(self.details)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0,2)
        splitter.setStretchFactor(1,3)
        queue.jobs_table.setMinimumHeight(72)
        queue.items_table.setMinimumHeight(100)
        # Let text columns share the available space instead of expanding every
        # status/date field to fit an entire history record.
        header=queue.jobs_table.horizontalHeader()
        for column,width in ((1,100),(2,110),(3,75),(4,160)):
            header.setSectionResizeMode(column,QHeaderView.ResizeMode.Interactive)
            queue.jobs_table.setColumnWidth(column,width)
        self.orientation=None
        page.installEventFilter(self)
        self.resize()

    def resize(self):
        # At the default 1180-wide window, full-width rows preserve readable job
        # columns. Large workspaces have enough width for two useful columns.
        orientation=Qt.Orientation.Horizontal if self.page.width()>=1280 else Qt.Orientation.Vertical
        if orientation!=self.orientation:
            self.orientation=orientation
            self.queue.jobs_splitter.setOrientation(orientation)
            self.queue.jobs_splitter.setSizes([500,500] if orientation==Qt.Orientation.Horizontal else [450,550])

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.Resize:
            self.resize()
            self.scroll_timer.start(0)
        return False

    def scroll_to_selection(self):
        for table in (self.queue.jobs_table,self.tabs.currentWidget()):
            if table and table.currentIndex().isValid():table.scrollTo(table.currentIndex())

    def show_child(self,child,selection=None,workflow=False):
        self.tabs.setTabText(0,'Steps' if workflow else 'Files')
        self.tabs.setTabVisible(1,bool(child))
        key=(selection,child) if child else None
        if child and key!=self.child_key:self.tabs.setCurrentIndex(1)
        self.child_key=key
        if not child:
            self.tabs.setCurrentIndex(0)
            self.queue.child_model.replace([],[])
