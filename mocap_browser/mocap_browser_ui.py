import sys
import os

from . import ui_utils
from .ui_utils import QtCore, QtWidgets, QtGui, QtOpenGL
from .resources import get_image_path

# Requires FBX SDK
import fbx, FbxCommon
from . import fbx_utils
from . import fbx_gl_utils

# Requires PyOpenGL
from OpenGL import GL
from .gl_utils import camera
from .gl_utils import scene_utils

from .qt_time_slider import TimeSliderWidget
from .qt_file_tree import QtFileTree, FolderConfig

standalone_app = None
if not QtWidgets.QApplication.instance():
    standalone_app = QtWidgets.QApplication(sys.argv)


class BaseViewportWidget(QtOpenGL.QGLWidget):
    def __init__(self, parent=None):
        QtOpenGL.QGLWidget.__init__(self, QtOpenGL.QGLFormat(QtOpenGL.QGL.SampleBuffers), parent)

        # viewport control things
        self.background_color = QtGui.QColor.fromRgb(80, 120, 150, 0.0)
        self.prev_mouse_x = 0
        self.prev_mouse_y = 0
        self.main_camera = camera.Camera()
        self.main_camera.setSceneRadius(100.0)
        self.reset_camera()

        ui_utils.add_hotkey(self, "R", self.reset_camera)
        ui_utils.add_hotkey(self, "F", self.reset_camera)
    
    def reset_camera(self):
        self.main_camera.reset(800, 500, 800)
        self.update()

    def initializeGL(self):
        self.qglClearColor(self.background_color)

    def paintGL(self):
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glLoadIdentity()
        self.main_camera.transform()
        scene_utils.draw_origin_grid()
        scene_utils.draw_axis_helper()

    def resizeGL(self, widthInPixels, heightInPixels):
        self.main_camera.setViewportDimensions(widthInPixels, heightInPixels)
        GL.glViewport(0, 0, widthInPixels, heightInPixels)

    def mousePressEvent(self, event):
        self.prev_mouse_x = event.x()
        self.prev_mouse_y = event.y()

    def mouseMoveEvent(self, event):
        """Viewport controls"""
        delta_x = event.x() - self.prev_mouse_x
        delta_y = event.y() - self.prev_mouse_y
        mouse_zoom_speed = 3

        # Orbit
        if event.buttons() == QtCore.Qt.LeftButton:
            self.main_camera.orbit(self.prev_mouse_x, self.prev_mouse_y, event.x(), event.y())

        # Zoom
        elif event.buttons() == QtCore.Qt.RightButton:
            self.main_camera.dollyCameraForward((delta_x + delta_y) * mouse_zoom_speed, False)

        # Panning
        elif event.buttons() == QtCore.Qt.MidButton:
            self.main_camera.translateSceneRightAndUp(delta_x, -delta_y)

        self.prev_mouse_x = event.x()
        self.prev_mouse_y = event.y()
        self.update()

    def wheelEvent(self, event):
        zoom_multiplier = 0.5
        self.main_camera.dollyCameraForward(event.delta() * zoom_multiplier, False)
        self.update()


class AnimationViewportWidget(BaseViewportWidget):

    frame_changed = QtCore.Signal(int)

    def __init__(self, parent):
        super().__init__(parent)

        self.play_active = False
        self.active_frame = 0
        self.start_frame = 0
        self.end_frame = 0

        # frame timer
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.play_next_frame)
        self.timer.start(30)

    def timerEvent(self, event):
        self.update()

    def toggle_play(self):
        self.play_active = not self.play_active

        if self.play_active:
            self.startTimer(1)

    def set_frame(self, frame):
        if self.play_active:
            return
        self.active_frame = frame
        self.update()

    def play_next_frame(self):
        if not self.play_active:
            return
        
        if self.active_frame >= self.end_frame:
            self.set_active_frame(self.start_frame)
        else:
            self.set_active_frame(self.active_frame + 1)
    
    def increment_frame(self, value=1):
        target_frame = self.active_frame + value
        target_frame = max(self.start_frame, min(target_frame, self.end_frame)) # clamp within timeline
        self.set_active_frame(target_frame)
    
    def go_to_start_frame(self):
        self.set_active_frame(self.start_frame)
    
    def go_to_end_frame(self):
        self.set_active_frame(self.end_frame)
    
    def set_active_frame(self, value):
        self.active_frame = value
        self.update()
        self.frame_changed.emit(self.active_frame)
        

class ViewportSceneDescription(object):
    def __init__(self):
        self.transform_hierarchy = {}


class FBXViewportWidget(AnimationViewportWidget):

    scene_content_updated = QtCore.Signal(ViewportSceneDescription)

    def __init__(self, parent):
        super().__init__(parent)

        self.fbx_handlers = []
        self.time = fbx.FbxTime()

        self.hidden_nodes = []

        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasText():
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():  # only if file or link is dropped
            return
        
        fbx_paths = []
        for url in event.mimeData().urls():
            local_path = url.toLocalFile()
            if local_path.lower().endswith(".fbx"):
                fbx_paths.append(local_path)

        if not fbx_paths:
            QtWidgets.QMessageBox.warning(self, "Invalid Paths", "Could not find any .fbx's in the dropped files")
            return

        self.load_fbx_files(fbx_paths)

    def paintGL(self):
        super().paintGL()

        GL.glLineWidth(4.0)
        GL.glBegin(GL.GL_LINES)
        for fbx_handler in self.fbx_handlers: # type: fbx_utils.FbxHandler
            if not fbx_handler.is_loaded:
                continue

            hidden_nodes = fbx_handler.hidden_nodes

            self.time.SetTime(0, 0, 0, self.active_frame)

            # get skeleton at current frame
            skel_points = fbx_gl_utils.recursive_get_fbx_skeleton_positions(
                fbx_handler.scene.GetRootNode(), 
                self.time,
                fbx_handler.display_color,
                )

            # draw skeleton
            GL.glColor(*fbx_handler.display_color)
            for bone_name, pos_list in skel_points.items():
                if bone_name in hidden_nodes:
                    continue

                node_pos = pos_list[0]
                parent_pos = pos_list[1]
                GL.glVertex(node_pos[0], node_pos[1], node_pos[2])
                GL.glVertex(parent_pos[0], parent_pos[1], parent_pos[2])
        GL.glEnd()
    
    def load_fbx_files(self, fbx_file_paths=None):
        if not fbx_file_paths:
            return

        self.remove_existing_handlers()

        if not isinstance(fbx_file_paths, list):
            fbx_file_paths = [fbx_file_paths]

        scene_desc = ViewportSceneDescription()

        start_times = []
        end_times = []
        for fbx_file in fbx_file_paths:

            if not os.path.exists(fbx_file):
                print(f"Failed to find fbx file: {fbx_file}")
                continue

            fbx_handler = fbx_utils.FbxHandler()
            fbx_handler.load_scene(fbx_file)
            self.fbx_handlers.append(fbx_handler)
            start_times.append(fbx_handler.get_start_frame())
            end_times.append(fbx_handler.get_end_frame())

            # assign random color to distinguish multiple clips
            if len(fbx_file_paths) > 1:
                fbx_handler.display_color = ui_utils.get_random_color()
            
            # send scene data to tree widget
            scene_hiearchy = fbx_utils.recursive_get_fbx_skeleton_hierarchy(
                fbx_handler.scene.GetRootNode(),
                )
            scene_desc.transform_hierarchy[fbx_file] = scene_hiearchy

        self.start_frame = min(start_times)
        self.end_frame = max(end_times)
        self.active_frame = self.start_frame
        self.scene_content_updated.emit(scene_desc)
        self.update()
    
    def remove_existing_handlers(self):
        for handler in self.fbx_handlers: # type: fbx_utils.FbxHandler
            handler.unload_scene()
        self.fbx_handlers.clear()
    
    def set_node_visibility(self, fbx_path, node_names, state):
        for fbx_handler in self.fbx_handlers: # type: fbx_utils.FbxHandler
            if fbx_handler.file_path != fbx_path:
                continue

            if state:
                for node in node_names:
                    if node in fbx_handler.hidden_nodes:
                        fbx_handler.hidden_nodes.remove(node)
            else:
                for node in node_names:
                    fbx_handler.hidden_nodes.append(node)
        
        self.update()


class MocapBrowserViewportWidget(QtWidgets.QWidget):
    def __init__(self, parent):
        super().__init__(parent)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.setLayout(self.main_layout)

        # OpenGL Widget
        self.fbx_viewport = FBXViewportWidget(self)
        self.main_layout.addWidget(self.fbx_viewport)

        # time slider
        self.timeline = TimeSliderWidget(precision=0)
        self.timeline.setMaximumHeight(30)
        self.main_layout.addWidget(self.timeline)
        
        # connect signals
        self.timeline.value_changed.connect(self.fbx_viewport.set_frame)
        self.fbx_viewport.frame_changed.connect(self.timeline.set_value)
        self.fbx_viewport.scene_content_updated.connect(self.update_timeline_from_loaded_fbxs)

        # create hotkeys
        ui_utils.add_hotkey(self, "Left", lambda: self.fbx_viewport.increment_frame(-1))
        ui_utils.add_hotkey(self, "Right", lambda: self.fbx_viewport.increment_frame(1))
        ui_utils.add_hotkey(self, "Shift+Left", lambda: self.fbx_viewport.increment_frame(-15))
        ui_utils.add_hotkey(self, "Shift+Right", lambda: self.fbx_viewport.increment_frame(15))
        ui_utils.add_hotkey(self, "Home", self.fbx_viewport.go_to_start_frame)
        ui_utils.add_hotkey(self, "End", self.fbx_viewport.go_to_end_frame)
        ui_utils.add_hotkey(self, "Space", self.fbx_viewport.toggle_play)

    def load_fbx_files(self, fbx_paths=None):
        self.fbx_viewport.load_fbx_files(fbx_paths)

    def update_timeline_from_loaded_fbxs(self, _):
        self.timeline.set_minimum(self.fbx_viewport.start_frame)
        self.timeline.set_maximum(self.fbx_viewport.end_frame)
        self.timeline.set_value(self.fbx_viewport.active_frame)
        self.timeline.reset_selection()


class MocapSkeletonTree(QtWidgets.QWidget):

    set_node_visibility = QtCore.Signal(str, list, bool)

    def __init__(self, parent):
        super().__init__(parent)

        self.main_layout = QtWidgets.QVBoxLayout()

        self.tree_widget = QtWidgets.QTreeWidget()
        self.tree_widget.setColumnCount(1)
        self.tree_widget.setIndentation(10)
        self.tree_widget.setHeaderHidden(True)
        self.tree_widget.itemClicked.connect(self.tree_item_check_changed)

        self.main_layout.addWidget(self.tree_widget)
        self.main_layout.setContentsMargins(2, 2, 2, 2)

        self.setLayout(self.main_layout)
    
    def populate_skeleton_tree(self, viewport_scene):
        if 0:
            viewport_scene = ViewportSceneDescription()
        
        self.tree_widget.clear()
        for fbx_file, scene_data in viewport_scene.transform_hierarchy.items():
            root_widget = QtWidgets.QTreeWidgetItem(self.tree_widget.invisibleRootItem())
            root_widget.setText(0, os.path.basename(fbx_file))
            root_widget.setText(1, fbx_file)
            root_widget.setCheckState(0, QtCore.Qt.CheckState.Checked)

            node_widgets = {}
            for child_name, parent_name in scene_data.items():
                parent_widget = node_widgets.get(parent_name, root_widget)
                widget_item = QtWidgets.QTreeWidgetItem(parent_widget)
                widget_item.setText(0, child_name)
                widget_item.setText(1, fbx_file)
                widget_item.setCheckState(0, QtCore.Qt.CheckState.Checked)
                node_widgets[child_name] = widget_item

        if len(viewport_scene.transform_hierarchy.keys()) == 1:
            self.tree_widget.expandAll()

    def tree_item_check_changed(self, widget):
        if 0:
            widget = QtWidgets.QTreeWidgetItem()

        fbx_path = widget.text(1)
        state = widget.checkState(0)
        node_names = ui_utils.recursive_set_checkstate(widget, state)
        self.set_node_visibility.emit(fbx_path, node_names, state is QtCore.Qt.CheckState.Checked)


class FBXFolderConfig(FolderConfig):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.file_icon = ui_utils.create_qicon(get_image_path("fbx_icon"))


class MocapFileTree(QtWidgets.QWidget):

    file_double_clicked = QtCore.Signal(str)

    def __init__(self, parent):
        super().__init__(parent)

        self.folder_path = QtWidgets.QLineEdit()
        self.folder_path.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.folder_path.setPlaceholderText("Root folder")

        self.search_line_edit = QtWidgets.QLineEdit()
        self.search_line_edit.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.search_line_edit.setPlaceholderText("Search...")
        self.search_line_edit.textChanged.connect(self._set_filter)

        self.set_folder_button = QtWidgets.QPushButton("...")
        self.set_folder_button.clicked.connect(self.set_active_folder)

        self.file_tree = QtFileTree()
        self.file_tree.setHeaderHidden(True)
        self.file_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)

        # tree config
        self.file_tree.default_folder_config_cls = FBXFolderConfig
        self.file_tree.file_double_clicked.connect(self.file_double_clicked)

        self.main_layout = QtWidgets.QVBoxLayout()
        file_line_layout = QtWidgets.QHBoxLayout()
        file_line_layout.addWidget(self.folder_path)
        file_line_layout.addWidget(self.set_folder_button)

        self.main_layout.addLayout(file_line_layout)
        self.main_layout.addWidget(self.search_line_edit)
        self.main_layout.addWidget(self.file_tree)
        self.main_layout.setContentsMargins(2, 2, 2, 2)

        self.setLayout(self.main_layout)

    def _set_filter(self):
        self.file_tree.set_filter(self.search_line_edit.text())

    def set_active_folder(self, folder_path=None):
        if not folder_path:
            folder_path = QtWidgets.QFileDialog.getExistingDirectory(
                self,
                "Choose Mocap Folder",
                dir=self.get_folder(),
                )
        if not folder_path:
            return
        
        self._set_folder(folder_path)
    
    def get_selected_paths(self):
        return self.file_tree.get_selected_file_paths()

    def get_folder(self):
        return self.folder_path.text()

    def _set_folder(self, folder_path):
        self.file_tree.set_folder(folder_path, file_exts=[".fbx"])
        self.folder_path.setText(folder_path)


class MocapBrowserWindow(ui_utils.ToolWindow):
    def __init__(self):
        super(MocapBrowserWindow, self).__init__()
        main_widget = QtWidgets.QWidget()
        main_layout = QtWidgets.QHBoxLayout()
        main_widget.setLayout(main_layout)
        self.setWindowTitle("Mocap Browser")

        main_splitter = QtWidgets.QSplitter()
        self.file_tree = MocapFileTree(self)
        self.viewport = MocapBrowserViewportWidget(self)
        self.skeleton_tree = MocapSkeletonTree(self)
        main_splitter.addWidget(self.file_tree)
        main_splitter.addWidget(self.viewport)
        main_splitter.addWidget(self.skeleton_tree)
        main_splitter.setStretchFactor(0, 0.7)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([250, 600, 0])

        # connect signals between widgets
        self.file_tree.file_double_clicked.connect(self.viewport.load_fbx_files)
        self.file_tree.file_tree.customContextMenuRequested.connect(self.context_menu)
        self.viewport.fbx_viewport.scene_content_updated.connect(self.skeleton_tree.populate_skeleton_tree)
        self.skeleton_tree.set_node_visibility.connect(self.viewport.fbx_viewport.set_node_visibility)

        main_layout.addWidget(main_splitter)
        self.setCentralWidget(main_widget)
        self.context_menu_actions = [
            {"Load all selected": self.load_all_selected},
        ]

    def context_menu(self):
        return ui_utils.build_menu_from_action_list(self.context_menu_actions)

    def load_all_selected(self):
        self.viewport.load_fbx_files(self.file_tree.get_selected_paths())

    
def main(refresh=False, active_folder=None):
    win = MocapBrowserWindow()
    win.main(refresh=refresh)
    win.resize(QtCore.QSize(720, 480))
    
    if active_folder:
        if os.path.exists(active_folder):
            win.file_tree.set_active_folder(active_folder)

    if standalone_app:
        ui_utils.standalone_app_window = win
        from .resources import stylesheets
        stylesheets.apply_standalone_stylesheet()
        sys.exit(standalone_app.exec_())


if __name__ == "__main__":
    main()
