import os

import numpy as np
import processing
from osgeo import gdal
from qgis.PyQt.QtGui import QIcon
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtWidgets import QMessageBox, QComboBox, QLineEdit
from qgis.PyQt.QtWidgets import QFileDialog, QDialog, QHBoxLayout, QVBoxLayout, QLabel, QSpinBox, QPushButton
from qgis.core import (
    QgsApplication, QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry,
    QgsMapLayer, QgsMessageLog, QgsProject, QgsTask, QgsVectorFileWriter,
    QgsVectorLayer, Qgis
)

try:
    from qgis.PyQt.QtCore import QVariant
    DOUBLE_FIELD_TYPE = QVariant.Double
except (ImportError, AttributeError):
    from qgis.PyQt.QtCore import QMetaType
    DOUBLE_FIELD_TYPE = QMetaType.Type.Double


POLYGON_GEOMETRY = getattr(Qgis, "GeometryType", None)
POLYGON_GEOMETRY = POLYGON_GEOMETRY.Polygon if POLYGON_GEOMETRY else 2


def is_polygon_layer(layer):
    return layer.type() == QgsMapLayer.VectorLayer and layer.geometryType() == POLYGON_GEOMETRY


MESSAGE_CATEGORY = 'HighGround'

class ProcTask(QgsTask):

    def __init__(self, description, raster_layer, vector_layer, new_layer_name, perc_val, low_perc_val, transform_context):
        super().__init__(description, QgsTask.CanCancel)
        self.transform_context = transform_context
        self.raster_layer = raster_layer
        self.vector_layer = vector_layer
        self.new_layer_name = new_layer_name
        self.perc_val = perc_val
        self.low_perc_val = low_perc_val
        self.exception = None

    def run(self):
        """Calculate percentiles and write the output shapefile."""
        QgsMessageLog.logMessage(
            f'Started "{self.description()}"',
            MESSAGE_CATEGORY,
            Qgis.Info
        )

        try:
            raster_layer = self.raster_layer
            vector_layer = self.vector_layer
            original_crs = vector_layer.crs()

            output_layer = QgsVectorLayer(f"Polygon?crs={original_crs.authid()}", "temp_results", "memory")
            output_data = output_layer.dataProvider()

            new_fields = vector_layer.fields().toList() + [
                QgsField(f"pct_{self.perc_val}", DOUBLE_FIELD_TYPE),
                QgsField(f"pct_{self.low_perc_val}", DOUBLE_FIELD_TYPE)
            ]
            output_data.addAttributes(new_fields)
            output_layer.updateFields()

            raster_crs = raster_layer.crs()
            transform_context = self.transform_context

            features_without_data = 0
            for idx, feature in enumerate(vector_layer.getFeatures()):
                original_geom = feature.geometry()
                attributes = feature.attributes()

                self.setProgress(int((idx + 1) / vector_layer.featureCount() * 100))

                if self.isCanceled():
                    return False

                processing_geom = QgsGeometry(original_geom)
                if original_crs != raster_crs:
                    xform = QgsCoordinateTransform(original_crs, raster_crs, transform_context)
                    processing_geom.transform(xform)

                percentile = None
                low_pct = None

                raster_extent = raster_layer.extent()
                geometry_extent = processing_geom.boundingBox()
                overlaps_raster = (
                    not processing_geom.isEmpty()
                    and geometry_extent.xMaximum() > raster_extent.xMinimum()
                    and geometry_extent.xMinimum() < raster_extent.xMaximum()
                    and geometry_extent.yMaximum() > raster_extent.yMinimum()
                    and geometry_extent.yMinimum() < raster_extent.yMaximum()
                )

                if overlaps_raster:
                    temp_vector = QgsVectorLayer(f"Polygon?crs={raster_crs.authid()}", "temp", "memory")
                    temp_data = temp_vector.dataProvider()
                    temp_feature = QgsFeature()
                    temp_feature.setGeometry(processing_geom)
                    temp_data.addFeatures([temp_feature])
                    temp_vector.updateExtents()

                    params = {
                        'INPUT': raster_layer,
                        'MASK': temp_vector,
                        'CROP_TO_CUTLINE': True,
                        'KEEP_RESOLUTION': True,
                        'NODATA': None,
                        'OUTPUT': 'TEMPORARY_OUTPUT'
                    }
                    clipped = processing.run("gdal:cliprasterbymasklayer", params)['OUTPUT']

                    ds = gdal.Open(clipped)
                    if ds is None:
                        raise RuntimeError(f"Could not open clipped raster for feature {idx}")
                    band = ds.GetRasterBand(1)
                    data = band.ReadAsArray()
                    if data is not None:
                        data_flat = data.reshape(-1)
                        data_flat = data_flat[np.isfinite(data_flat)]
                        nodata = band.GetNoDataValue()
                        if nodata is not None:
                            data_flat = data_flat[data_flat != nodata]
                        if data_flat.size > 0:
                            percentile = float(np.percentile(data_flat, self.perc_val))
                            low_pct = float(np.percentile(data_flat, self.low_perc_val))
                    ds = None

                if percentile is None:
                    features_without_data += 1

                new_feature = QgsFeature(output_layer.fields())
                # Preserve the source geometry; only the processing copy is transformed.
                new_feature.setGeometry(original_geom)
                new_feature.setAttributes(attributes + [percentile, low_pct])
                if not output_data.addFeature(new_feature):
                    raise RuntimeError(f"Could not add feature {idx} to the output layer")

            if features_without_data:
                QgsMessageLog.logMessage(
                    f"{features_without_data} of {vector_layer.featureCount()} features had no valid raster pixels; "
                    "their percentile fields are NULL.",
                    MESSAGE_CATEGORY,
                    Qgis.Warning
                )

            save_options = QgsVectorFileWriter.SaveVectorOptions()
            save_options.driverName = "ESRI Shapefile"
            save_options.fileEncoding = "UTF-8"

            if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
                writer = QgsVectorFileWriter.writeAsVectorFormatV3(
                    output_layer,
                    self.new_layer_name,
                    transform_context,
                    save_options
                )
            else:
                writer = QgsVectorFileWriter.writeAsVectorFormatV2(
                    output_layer,
                    self.new_layer_name,
                    transform_context,
                    save_options
                )

            if writer[0] != QgsVectorFileWriter.NoError:
                raise RuntimeError(f"Failed to save output file: {writer[1]}")

        except Exception as e:
            self.exception = e
            return False

        return True

    def finished(self, result):
        """Load the output layer after processing on the main thread."""
        if not result:
            if self.exception is None:
                QgsMessageLog.logMessage(
                    f'"{self.description()}" was canceled or stopped without an exception.',
                    MESSAGE_CATEGORY,
                    Qgis.Warning
                )
            else:
                QgsMessageLog.logMessage(
                    f'"{self.description()}" failed: {self.exception}',
                    MESSAGE_CATEGORY,
                    Qgis.Critical
                )
                raise self.exception
            return

        result_layer = QgsVectorLayer(self.new_layer_name, "Percentile Results", "ogr")
        if not result_layer.isValid():
            error = RuntimeError(
                f"Output file could not be loaded as a vector layer: {self.new_layer_name}"
            )
            self.exception = error
            QgsMessageLog.logMessage(str(error), MESSAGE_CATEGORY, Qgis.Critical)
            raise error

        QgsProject.instance().addMapLayer(result_layer)
        result_layer.triggerRepaint()
        QgsMessageLog.logMessage(
            f'"{self.description()}" completed and loaded {self.new_layer_name}.',
            MESSAGE_CATEGORY,
            Qgis.Success
        )

class LayerSelectionDialog(QDialog):

    def __init__(self, vector_layers, raster_layers, parent=None):
        super().__init__(parent)

        self.setWindowTitle('Select Inputs and Parameters')

        self.layout = QVBoxLayout(self)

        self.dem_label = QLabel('Select DEM (Raster Layer):')
        self.layout.addWidget(self.dem_label)
        self.raster_combo = QComboBox()
        for name, layer_id in raster_layers:
            self.raster_combo.addItem(name, layer_id)
        self.layout.addWidget(self.raster_combo)

        self.vector_label = QLabel('Select Shapefile (Vector Layer):')
        self.layout.addWidget(self.vector_label)
        self.vector_combo = QComboBox()
        for name, layer_id in vector_layers:
            self.vector_combo.addItem(name, layer_id)
        self.layout.addWidget(self.vector_combo)

        self.percentile_label = QLabel('Select high percentile:')
        self.layout.addWidget(self.percentile_label)
        self.percentile_selection = QSpinBox()
        self.percentile_selection.setValue(99)
        self.percentile_selection.setRange(1, 99)
        self.layout.addWidget(self.percentile_selection)

        self.low_percentile_label = QLabel('Select low percentile:')
        self.layout.addWidget(self.low_percentile_label)
        self.low_percentile_selection = QSpinBox()
        self.low_percentile_selection.setValue(1)
        self.low_percentile_selection.setRange(1, 99)
        self.layout.addWidget(self.low_percentile_selection)

        self.output_label = QLabel('Choose Output Shapefile:')
        self.layout.addWidget(self.output_label)
        output_layout = QHBoxLayout()
        self.output_file = QLineEdit()
        self.output_file.setPlaceholderText("Path to output shapefile")
        output_layout.addWidget(self.output_file)
        self.save_as_button = QPushButton('...')
        output_layout.addWidget(self.save_as_button)
        self.layout.addLayout(output_layout)
        self.save_as_button.clicked.connect(self.output_file_dialog)

        proc_button = QPushButton("Process")
        proc_button.clicked.connect(self.accept)
        self.layout.addWidget(proc_button)

        self.setLayout(self.layout)

    def output_file_dialog(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Shapefile", self.output_file.text().strip() or "percentile_results.shp",
            "ESRI Shapefile (*.shp)"
        )
        if path:
            self.output_file.setText(path)

    def accept(self):
        if self.low_percentile_selection.value() >= self.percentile_selection.value():
            QMessageBox.warning(self, "HighGround", "The low percentile must be lower than the high percentile.")
            return

        path = self.output_file.text().strip()
        if not path or os.path.basename(path) in ("", ".", ".."):
            QMessageBox.warning(self, "HighGround", "Choose an output shapefile.")
            return

        extension = os.path.splitext(path)[1]
        if not extension:
            path += ".shp"
        elif extension.lower() != ".shp":
            QMessageBox.warning(self, "HighGround", "The output file must have a .shp extension.")
            return

        self.output_file.setText(path)
        super().accept()

    def get_selected_values(self):
        return (
            self.vector_combo.currentData(),
            self.raster_combo.currentData(),
            self.output_file.text().strip(),
            self.percentile_selection.value(),
            self.low_percentile_selection.value()
        )


def get_layer(layer_id):
    layer = QgsProject.instance().mapLayer(layer_id)
    if layer is None:
        raise ValueError("The selected layer is no longer in the project.")
    return layer


class HighGround:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.plugin_dir = os.path.dirname(__file__)
        self.icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.active_tasks = {}

    def initGui(self):
        self.action = QAction("HighGround", self.iface.mainWindow())
        self.action = QAction(QIcon(self.icon_path), "HighGround", self.iface.mainWindow())
        self.action.triggered.connect(self.select_layers)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&HighGround", self.action)

    def unload(self):
        self.iface.removePluginMenu("&HighGround", self.action)
        self.iface.removeToolBarIcon(self.action)

    def select_layers(self):
        layers = QgsProject.instance().mapLayers().values()

        vector_layers = [(layer.name(), layer.id()) for layer in layers if is_polygon_layer(layer)]
        raster_layers = [(layer.name(), layer.id()) for layer in layers if layer.type() == QgsMapLayer.RasterLayer]

        if not vector_layers:
            QMessageBox.warning(self.iface.mainWindow(), "HighGround", "No polygon vector layers loaded.")
            return
        if not raster_layers:
            QMessageBox.warning(self.iface.mainWindow(), "HighGround", "No raster layers loaded.")
            return

        dialog = LayerSelectionDialog(vector_layers, raster_layers, self.iface.mainWindow())
        if hasattr(dialog, "exec"):
            dialog_result = dialog.exec()
        else:
            dialog_result = dialog.exec_()

        accepted = getattr(QDialog, "Accepted", None)
        if accepted is None:
            accepted = QDialog.DialogCode.Accepted

        if dialog_result == accepted:
            vector_id, raster_id, new_layer_name, perc_val, low_perc_val = dialog.get_selected_values()
            try:
                vector_layer = get_layer(vector_id)
                raster_layer = get_layer(raster_id)
            except ValueError as e:
                QMessageBox.critical(self.iface.mainWindow(), 'HighGround', str(e))
                return

            tasky = ProcTask('HighGround Processing', raster_layer, vector_layer, new_layer_name, perc_val, low_perc_val,
                            QgsProject.instance().transformContext())
            task_id = id(tasky)
            self.active_tasks[task_id] = tasky
            release_task = lambda: self.active_tasks.pop(task_id, None)
            tasky.taskCompleted.connect(release_task)
            tasky.taskTerminated.connect(release_task)
            if not QgsApplication.taskManager().addTask(tasky):
                self.active_tasks.pop(task_id, None)
                message = 'Could not start HighGround processing.'
                QgsMessageLog.logMessage(message, MESSAGE_CATEGORY, Qgis.Critical)
                QMessageBox.critical(self.iface.mainWindow(), 'HighGround', message)
