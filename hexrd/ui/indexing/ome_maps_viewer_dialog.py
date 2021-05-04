import copy
import os

import matplotlib
from matplotlib.backends.backend_qt5agg import FigureCanvas
from matplotlib.figure import Figure
import numpy as np
import yaml

from PySide2.QtCore import Signal, QObject, QSignalBlocker, Qt
from PySide2.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QMessageBox,
    QSizePolicy, QSpinBox
)

from hexrd.constants import sigma_to_fwhm
from hexrd.findorientations import (
    clean_map, filter_maps_if_requested, filter_stdev_DFLT
)
from hexrd.imageutil import find_peaks_2d

from hexrd.ui import resource_loader

from hexrd.ui.color_map_editor import ColorMapEditor
from hexrd.ui.hexrd_config import HexrdConfig
from hexrd.ui.navigation_toolbar import NavigationToolbar
from hexrd.ui.select_items_widget import SelectItemsWidget
from hexrd.ui.utils import has_nan
from hexrd.ui.ui_loader import UiLoader

import hexrd.ui.constants
import hexrd.ui.resources.indexing


DEFAULT_FWHM = filter_stdev_DFLT * sigma_to_fwhm


class OmeMapsViewerDialog(QObject):

    accepted = Signal()
    rejected = Signal()

    def __init__(self, data, parent=None):
        super().__init__(parent)

        loader = UiLoader()
        self.ui = loader.load_file('ome_maps_viewer_dialog.ui', parent)

        self.data = data
        self.cmap = hexrd.ui.constants.DEFAULT_CMAP
        self.norm = None
        self.spots = None
        self.reset_internal_config()

        self.setup_widget_paths()

        self.setup_combo_box_item_data()

        # Hide the method tab bar. The user selects it via the combo box.
        self.ui.tab_widget.tabBar().hide()

        self.create_tooltips()

        self.setup_plot()
        self.setup_color_map()
        self.setup_hkls_table()

        self.update_gui()

        self.setup_connections()

        # This will trigger a re-draw and update of everything
        self.filter_modified()

    def setup_connections(self):
        self.ui.active_hkl.currentIndexChanged.connect(self.update_plot)
        self.ui.label_spots.toggled.connect(self.update_spots)
        self.ui.export_button.pressed.connect(self.on_export_button_pressed)
        self.ui.accepted.connect(self.on_accepted)
        self.ui.rejected.connect(self.on_rejected)

        self.ui.method.currentIndexChanged.connect(self.update_method_tab)
        self.ui.method.currentIndexChanged.connect(self.update_config)
        self.ui.method.currentIndexChanged.connect(self.update_spots)
        self.color_map_editor.ui.minimum.valueChanged.connect(
            self.update_config)

        self.select_hkls_widget.selection_changed.connect(self.update_config)

        # A plot reset is needed for log scale to handle the NaN values
        self.color_map_editor.ui.log_scale.toggled.connect(self.update_plot)

        def changed_signal(w):
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                return w.valueChanged
            elif isinstance(w, QComboBox):
                return w.currentIndexChanged
            elif isinstance(w, QCheckBox):
                return w.toggled
            else:
                raise Exception(f'Unhandled widget type: {type(w)}')

        for w in self.filter_widgets:
            changed_signal(w).connect(self.filter_modified)

        for w in self.yaml_widgets:
            changed_signal(w).connect(self.update_config)

        for w in self.method_parameter_widgets:
            changed_signal(w).connect(self.update_spots)

    def setup_combo_box_item_data(self):
        # Set the item data for the combo boxes to be the names we want
        item_data = [
            'label',
            'blob_dog',
            'blob_log'
        ]
        for i, data in enumerate(item_data):
            self.ui.method.setItemData(i, data)

        item_data = [
            'dbscan',
            'sph-dbscan',
            'ort-dbscan',
            'fclusterdata'
        ]
        for i, data in enumerate(item_data):
            self.ui.clustering_algorithm.setItemData(i, data)

    def create_tooltips(self):
        tooltip = ('Full width at half maximum (FWHM) to use for the Gaussian '
                   f'Laplace filter.\nDefault: {DEFAULT_FWHM:.2f}')
        self.ui.filtering_fwhm.setToolTip(tooltip)
        self.ui.filtering_fwhm_label.setToolTip(tooltip)

    def update_enable_states(self):
        filtering = self.ui.apply_filtering.isChecked()
        apply_gl = self.ui.filtering_apply_gaussian_laplace.isChecked()
        fwhm_enabled = filtering and apply_gl

        self.ui.filtering_apply_gaussian_laplace.setEnabled(filtering)
        self.ui.filtering_fwhm.setEnabled(fwhm_enabled)
        self.ui.filtering_fwhm_label.setEnabled(fwhm_enabled)

    def reset_internal_config(self):
        self.config = copy.deepcopy(HexrdConfig().indexing_config)

    def show(self):
        self.update_plot()
        self.ui.show()

    def on_accepted(self):
        # Update the config just in case...
        self.update_config()
        try:
            self.validate()
        except ValidationException as e:
            QMessageBox.critical(self.ui, 'HEXRD', str(e))
            self.ui.show()
            return

        self.save_config()
        self.accepted.emit()

    def on_rejected(self):
        self.rejected.emit()

    def validate(self):
        hkls = self.config['find_orientations']['seed_search']['hkl_seeds']
        if not hkls:
            raise ValidationException('No hkls selected')

    def setup_widget_paths(self):
        text = resource_loader.load_resource(hexrd.ui.resources.indexing,
                                             'gui_config_maps.yml')
        self.gui_config_maps = yaml.load(text, Loader=yaml.FullLoader)

        paths = {}

        def recursive_get_paths(cur_config, cur_path):
            for key, value in cur_config.items():
                new_path = cur_path + [key]
                if isinstance(value, str):
                    paths[value] = new_path
                    continue

                recursive_get_paths(value, new_path)

        initial_path = []
        recursive_get_paths(self.gui_config_maps, initial_path)
        self.widget_paths = paths

    @property
    def method_parameter_widgets(self):
        maps = self.gui_config_maps
        methods = maps['find_orientations']['seed_search']['method']
        names = [v for d in methods.values() for v in d.values()]
        return [getattr(self.ui, x) for x in names]

    @property
    def method_name(self):
        return self.ui.method.currentData()

    @method_name.setter
    def method_name(self, v):
        w = self.ui.method
        for i in range(w.count()):
            if v == w.itemData(i):
                w.setCurrentIndex(i)
                return

        raise Exception(f'Unable to set method: {v}')

    def filter_modified(self):
        self.update_enable_states()
        self.update_config()
        self.reset_filters()
        self.update_plot()

    @property
    def filter_widgets(self):
        return [
            self.ui.apply_filtering,
            self.ui.filtering_apply_gaussian_laplace,
            self.ui.filtering_fwhm,
        ]

    @property
    def filter_maps(self):
        if not self.ui.apply_filtering.isChecked():
            return False

        if not self.ui.filtering_apply_gaussian_laplace.isChecked():
            return True

        # Keep this as a native type...
        return float(self.ui.filtering_fwhm.value() / sigma_to_fwhm)

    @filter_maps.setter
    def filter_maps(self, v):
        if isinstance(v, bool):
            filtering = v
            apply_gl = False
            fwhm = DEFAULT_FWHM
        else:
            filtering = True
            apply_gl = True
            fwhm = v * sigma_to_fwhm

        self.ui.apply_filtering.setChecked(filtering)
        self.ui.filtering_apply_gaussian_laplace.setChecked(apply_gl)
        self.ui.filtering_fwhm.setValue(fwhm)

    def update_method_tab(self):
        # Take advantage of the naming scheme...
        method_tab = getattr(self.ui, self.method_name + '_tab')
        self.ui.tab_widget.setCurrentWidget(method_tab)

    @property
    def hkls(self):
        hkl_indices = self.data.iHKLList
        all_hkls = self.data.planeData.getHKLs(asStr=True)
        return [all_hkls[i] for i in hkl_indices]

    def update_hkl_options(self):
        # This won't trigger a re-draw. Can change in the future if needed.
        blocker = QSignalBlocker(self.ui.active_hkl)  # noqa: F841
        self.ui.active_hkl.clear()
        self.ui.active_hkl.addItems(self.hkls)

    def setup_plot(self):
        # Create the figure and axes to use
        canvas = FigureCanvas(Figure(tight_layout=True))

        # Get the canvas to take up the majority of the screen most of the time
        canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        fig = canvas.figure
        ax = fig.add_subplot()
        ax.set_title('Eta Omega Maps')
        ax.set_xlabel(r'$\eta$ ($\deg$)')
        ax.set_ylabel(r'$\omega$ ($\deg$)')
        self.ui.canvas_layout.addWidget(canvas)

        self.toolbar = NavigationToolbar(canvas, self.ui, False)
        self.ui.canvas_layout.addWidget(self.toolbar)

        # Center the toolbar
        self.ui.canvas_layout.setAlignment(self.toolbar, Qt.AlignCenter)

        self.fig = fig
        self.ax = ax
        self.canvas = canvas

    def setup_color_map(self):
        self.color_map_editor = ColorMapEditor(self, self.ui)
        self.ui.color_map_editor_layout.addWidget(self.color_map_editor.ui)
        self.update_cmap_bounds()

        # Set the initial max as 20
        self.color_map_editor.ui.maximum.setValue(20)

    def setup_hkls_table(self):
        selected = self.config['find_orientations']['seed_search']['hkl_seeds']
        hkls = self.hkls
        items = [(x, i in selected) for i, x in enumerate(hkls)]
        self.select_hkls_widget = SelectItemsWidget(items, self.ui)
        layout = self.ui.select_hkls_widget_layout
        layout.addWidget(self.select_hkls_widget.ui)

        # Fix the height so it doesn't take up too much space
        size = layout.sizeHint()
        height_adjustment = min(3, len(hkls)) / len(hkls)
        size.setHeight(size.height() * height_adjustment)
        layout.parentWidget().setFixedSize(size)

    @property
    def selected_hkls(self):
        return self.select_hkls_widget.selected_indices

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, d):
        if hasattr(self, '_data') and d == self._data:
            return

        # Make a deep copy, and clean the data. Hexrd will do this
        # cleaning on the original data on its own in
        # generate_orientation_fibers().
        self.raw_data = copy.deepcopy(d)
        self.cleaned_data = copy.deepcopy(d)
        for map in self.cleaned_data.dataStore:
            clean_map(map)

        # This data will have filters applied to it
        # We will make a shallow copy, and deep copy the data store
        # when filters are applied.
        self._data = copy.copy(self.cleaned_data)
        self.reset_filters()

        self.update_extent()
        self.update_cmap_bounds()

    @property
    def image_data(self):
        return self.data.dataStore[self.current_hkl_index]

    def update_extent(self):
        etas = np.degrees(self.data.etas)
        omes = np.degrees(self.data.omegas)
        self.extent = (etas[0], etas[-1], omes[-1], omes[0])

    def update_cmap_bounds(self):
        if not hasattr(self, 'color_map_editor'):
            return

        self.color_map_editor.update_bounds(self.image_data)

        w = self.color_map_editor.ui.minimum
        w.setStyleSheet('background-color: yellow')
        note = 'NOTE: this is used to set find_orientations:threshold'
        if note not in w.toolTip():
            w.setToolTip(f'{w.toolTip()}\n\n{note}')

    @property
    def display_spots(self):
        return self.ui.label_spots.isChecked()

    def clear_spot_lines(self):
        if hasattr(self, '_spot_lines'):
            self._spot_lines.remove()
            del self._spot_lines

    def update_spots(self):
        self.clear_spot_lines()
        if not self.display_spots:
            self.draw_idle()
            return

        self.create_spots()
        if self.spots.size:
            kwargs = {
                'x': self.spots[:, 1],
                'y': self.spots[:, 0],
                's': 18,
                'c': 'm',
                'marker': '+',
            }
            self._spot_lines = self.ax.scatter(**kwargs)

        self.draw_idle()

    def update_plot(self):
        ax = self.ax

        data = self.image_data
        if isinstance(self.norm, matplotlib.colors.LogNorm) and has_nan(data):
            # The log norm can't handle NaNs. Set them to -1.
            data = copy.deepcopy(data)
            data[np.isnan(data)] = -1

        if not hasattr(self, 'im'):
            im = ax.imshow(data)
            self.im = im
            self.original_extent = im.get_extent()
        else:
            im = self.im
            im.set_data(data)

        im.set_cmap(self.cmap)
        im.set_norm(self.norm)

        self.update_spots()

        im.set_extent(self.extent)

        ax.relim()
        ax.autoscale_view()
        ax.axis('auto')

        self.draw_idle()

    def draw_idle(self):
        self.fig.canvas.draw_idle()

    @property
    def current_hkl_index(self):
        return self.ui.active_hkl.currentIndex()

    def set_cmap(self, cmap):
        self.cmap = cmap
        if hasattr(self, 'im'):
            self.im.set_cmap(cmap)
            self.draw_idle()

    def set_norm(self, norm):
        self.norm = norm
        if hasattr(self, 'im'):
            self.im.set_norm(norm)
            self.draw_idle()

    def create_spots(self):
        data = self.image_data

        if has_nan(data):
            # Get rid of nans to make our work easier
            data = copy.deepcopy(data)
            data[np.isnan(data)] = 0

        method_name = self.method_name
        method_dict = self.config['find_orientations']['seed_search']['method']
        method_kwargs = method_dict[method_name]

        _, spots = find_peaks_2d(data, method_name, method_kwargs)

        if spots.size:
            # Rescale the points to match the extents
            old_extent = self.original_extent
            old_x_range = (old_extent[0], old_extent[1])
            old_y_range = (old_extent[3], old_extent[2])
            new_x_range = (self.extent[0], self.extent[1])
            new_y_range = (self.extent[3], self.extent[2])

            spots[:, 1] = np.interp(spots[:, 1], old_x_range, new_x_range)
            spots[:, 0] = np.interp(spots[:, 0], old_y_range, new_y_range)

        self.spots = spots

    def on_export_button_pressed(self):
        selected_file, selected_filter = QFileDialog.getSaveFileName(
            self.ui, 'Export Eta Omega Maps', HexrdConfig().working_dir,
            'NPZ files (*.npz)')

        if selected_file:
            HexrdConfig().working_dir = os.path.dirname(selected_file)
            if not selected_file.endswith('.npz'):
                selected_file += '.npz'

            # Save the raw data out...
            self.raw_data.save(selected_file)

    @property
    def threshold(self):
        return self.color_map_editor.ui.minimum.value()

    @threshold.setter
    def threshold(self, v):
        if self.color_map_editor.ui.maximum.value() <= v:
            # Move the maximum so we can set the minimum also
            self.color_map_editor.ui.maximum.setValue(v + 1)

        self.color_map_editor.ui.minimum.setValue(v)

    @property
    def yaml_widgets(self):
        return [getattr(self.ui, x) for x in self.widget_paths.keys()]

    @property
    def all_widgets(self):
        return self.yaml_widgets + self.filter_widgets + [
            self.ui.method,
            self.ui.tab_widget,
            self.ui.active_hkl
        ]

    def update_gui(self):
        # Updates all of the widgets with their settings from the config
        self.update_hkl_options()
        blockers = [QSignalBlocker(x) for x in self.all_widgets]  # noqa: F841

        def setter(w):
            if isinstance(w, QComboBox):
                return lambda x: w.setCurrentIndex(w.findData(x))

            # Assume it is a spin box of some kind
            return w.setValue

        config = self.config

        def set_val(w, path):
            cur = config
            for x in path:
                if x not in cur:
                    # If it's not in the config, skip over it
                    return
                cur = cur[x]

            setter(w)(cur)

        for w, path in self.widget_paths.items():
            w = getattr(self.ui, w)
            set_val(w, path)

        find_orientations = config['find_orientations']
        # Update the method name
        method = find_orientations['seed_search']['method']
        self.method_name = next(iter(method))
        self.update_method_tab()

        # Also set the color map minimum to the threshold value...
        self.threshold = find_orientations['threshold']

        self.filter_maps = find_orientations['orientation_maps']['filter_maps']

    def update_config(self):
        # Update all of the config with their settings from the widgets
        config = self.config
        find_orientations = config['find_orientations']

        # Clear the method so it can be set to a different one
        method = find_orientations['seed_search']['method']
        method.clear()

        # Give it some dummy contents so the setter below will run
        method_name = self.method_name
        dummy_method = (
            self.gui_config_maps['find_orientations']['seed_search']['method'])
        method[method_name] = copy.deepcopy(dummy_method[method_name])

        def getter(w):
            if isinstance(w, QComboBox):
                return w.currentData

            # Assume it is a spin box of some kind
            return w.value

        def set_val(val, path):
            cur = config
            for x in path[:-1]:
                if x not in cur:
                    # If it's not in the config, skip over it
                    return
                cur = cur[x]

            cur[path[-1]] = val

        for w, path in self.widget_paths.items():
            w = getattr(self.ui, w)
            val = getter(w)()
            set_val(val, path)

        # Also set the threshold to the minimum color map value...
        find_orientations['threshold'] = self.threshold
        find_orientations['seed_search']['hkl_seeds'] = self.selected_hkls
        find_orientations['orientation_maps']['filter_maps'] = self.filter_maps

    def save_config(self):
        HexrdConfig().config['indexing'] = copy.deepcopy(self.config)

    def reset_filters(self):
        # Reset the data store
        if hasattr(self.data, '_dataStore'):
            name = '_dataStore'
        else:
            name = 'dataStore'
        setattr(self.data, name, copy.deepcopy(self.cleaned_data.dataStore))

        # Make a fake config to pass to hexrd
        class Cfg:
            pass

        path = ['find_orientations', 'orientation_maps', 'filter_maps']
        cfg = Cfg()
        cur = cfg
        for name in path[:-1]:
            setattr(cur, name, Cfg())
            cur = getattr(cur, name)

        setattr(cur, path[-1], self.filter_maps)

        # Perform the filtering
        filter_maps_if_requested(self.data, cfg)


class ValidationException(Exception):
    pass
