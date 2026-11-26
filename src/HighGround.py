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
from qgis.core import QgsProject, QgsMapLayer, QgsTask, QgsMessageLog
from qgis.core import (QgsVectorLayer, QgsFeature, QgsField,
                       QgsCoordinateTransform, QgsVectorFileWriter, QgsGeometry)

try:
    from qgis.PyQt.QtCore import QVariant
    DOUBLE_FIELD_TYPE = QVariant.Double
except (ImportError, AttributeError):
    from qgis.PyQt.QtCore import QMetaType
    DOUBLE_FIELD_TYPE = QMetaType.Type.Double


from qgis.core import (
    QgsApplication, QgsTask, QgsMessageLog, Qgis
    )

MESSAGE_CATEGORY = 'HighGroundProcessing'


class ProcTask(QgsTask):

    def __init__(self, description, raster_layer, vector_layer, new_layer_name, perc_val, low_perc_val):
        super().__init__(description, QgsTask.CanCancel)
        self.raster_layer = raster_layer
        self.vector_layer = vector_layer
        self.new_layer_name = new_layer_name
        self.perc_val = perc_val
        self.low_perc_val = low_perc_val
        self.total = 0
        self.iterations = 0
        self.duration = 10  # You can set this to an appropriate value if needed
        self.exception = None

    def run(self):
        """Here you implement your heavy lifting.
        Should periodically test for isCanceled() to gracefully
        abort.
        This method MUST return True or False.
        Raising exceptions will crash QGIS, so we handle them
        internally and raise them in self.finished
        """
        QgsMessageLog.logMessage('Started task "{}"'.format(
                                     self.description()),
                                 MESSAGE_CATEGORY, Qgis.Info)


        QgsMessageLog.logMessage("Begin processing", 'HighGround')

        try:
            raster_layer = get_layer(self.raster_layer)
            vector_layer = get_layer(self.vector_layer)
            original_crs = vector_layer.crs()

            # Create output layer with proper CRS
            output_layer = QgsVectorLayer(f"Polygon?crs={original_crs.authid()}", "temp_results", "memory")
            output_data = output_layer.dataProvider()

            # Add fields (original + new)
            new_fields = vector_layer.fields().toList() + [
                QgsField(f"pct_{self.perc_val}", DOUBLE_FIELD_TYPE),
                QgsField(f"pct_{self.low_perc_val}", DOUBLE_FIELD_TYPE)
            ]
            output_data.addAttributes(new_fields)
            output_layer.updateFields()

            # CRS transformation setup
            raster_crs = raster_layer.crs()
            transform_context = QgsProject.instance().transformContext()

            # Processing loop
            for idx, feature in enumerate(vector_layer.getFeatures()):
                original_geom = feature.geometry()
                attributes = feature.attributes()

                self.setProgress(int((idx + 1) / vector_layer.featureCount() * 100))

                # check isCanceled() to handle cancellation
                if self.isCanceled():
                    return False

                # Create transformed geometry for raster processing
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
                    # Create temporary layer for clipping
                    temp_vector = QgsVectorLayer(f"Polygon?crs={raster_crs.authid()}", "temp", "memory")
                    temp_data = temp_vector.dataProvider()
                    temp_feature = QgsFeature()
                    temp_feature.setGeometry(processing_geom)
                    temp_data.addFeatures([temp_feature])
                    temp_vector.updateExtents()

                    # Clip raster
                    params = {
                        'INPUT': raster_layer,
                        'MASK': temp_vector,
                        'CROP_TO_CUTLINE': True,
                        'KEEP_RESOLUTION': True,
                        'NODATA': None,
                        'OUTPUT': 'TEMPORARY_OUTPUT'
                    }
                    clipped = processing.run("gdal:cliprasterbymasklayer", params)['OUTPUT']

                    # Calculate percentiles
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
                    QgsMessageLog.logMessage(
                        f"No valid raster pixels for feature {idx}; percentile values will be NULL.",
                        'HighGround',
                        Qgis.Warning
                    )

                # Create output feature with ORIGINAL geometry
                new_feature = QgsFeature(output_layer.fields())
                new_feature.setGeometry(original_geom)  # Use original untransformed geometry
                new_feature.setAttributes(attributes + [percentile, low_pct])
                output_data.addFeature(new_feature)

                QgsMessageLog.logMessage(f"Processed {idx + 1}/{vector_layer.featureCount()}", 'HighGround')

            # Save to shapefile
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

            if writer[0] == QgsVectorFileWriter.NoError:
                # Load the result and zoom to it
                result_layer = QgsVectorLayer(self.new_layer_name, "Percentile Results", "ogr")
                QgsProject.instance().addMapLayer(result_layer)
                result_layer.triggerRepaint()
                QgsMessageLog.logMessage(f"Success! Layer loaded at: {self.new_layer_name}", 'HighGround')
            else:
                QgsMessageLog.logMessage(
                    f"Failed to save output file: {writer[1]}", 'HighGround'
                )

        except Exception as e:
            QgsMessageLog.logMessage(f"Error: {str(e)}", 'HighGround')
            # DO NOT raise Exception('bad value!')
            # this would crash QGIS
            # self.exception = Exception('bad value!')
            # return False
            self.exception = e
            return False


        QgsMessageLog.logMessage('Completed task "{}"'.format(
                                             self.description()),
                                         MESSAGE_CATEGORY, Qgis.Info)
        return True

    def finished(self, result):
        """
        This function is automatically called when the task has
        completed (successfully or not).
        You implement finished() to do whatever follow-up stuff
        should happen after the task is complete.
        finished is always called from the main thread, so it's safe
        to do GUI operations and raise Python exceptions here.
        result is the return value from self.run.
        """
        if result:
            QgsMessageLog.logMessage(
                'RandomTask "{name}" completed\n' \
                'RandomTotal: {total} (with {iterations} '\
              'iterations)'.format(
                  name=self.description(),
                  total=self.total,
                  iterations=self.iterations),
              MESSAGE_CATEGORY, Qgis.Success)
        else:
            if self.exception is None:
                QgsMessageLog.logMessage(
                    'RandomTask "{name}" not successful but without '\
                    'exception (probably the task was manually '\
                    'canceled by the user)'.format(
                        name=self.description()),
                    MESSAGE_CATEGORY, Qgis.Warning)
            else:
                QgsMessageLog.logMessage(
                    'RandomTask "{name}" Exception: {exception}'.format(
                        name=self.description(),
                        exception=self.exception),
                    MESSAGE_CATEGORY, Qgis.Critical)
                raise self.exception

    def cancel(self):
        QgsMessageLog.logMessage(
            'RandomTask "{name}" was canceled'.format(
                name=self.description()),
            MESSAGE_CATEGORY, Qgis.Info)
        super().cancel()


class LayerSelectionDialog(QDialog):

    def __init__(self, vector_layers, raster_layers, parent=None):
        super().__init__(parent)

        self.setWindowTitle('Select Inputs and Parameters')

        self.layout = QVBoxLayout(self)

        self.dem_label = QLabel('Select DEM (Raster Layer):')
        self.layout.addWidget(self.dem_label)
        self.raster_combo = QComboBox()
        self.raster_combo.addItems(raster_layers)
        self.layout.addWidget(self.raster_combo)

        self.vector_label = QLabel('Select Shapefile (Vector Layer):')
        self.layout.addWidget(self.vector_label)
        self.vector_combo = QComboBox()
        self.vector_combo.addItems(vector_layers)
        self.layout.addWidget(self.vector_combo)

        self.percentile_label = QLabel('Select high percentile:')
        self.layout.addWidget(self.percentile_label)
        # Use QSpinBox for integer input between 1 and 99
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
            self.vector_combo.currentText(),
            self.raster_combo.currentText(),
            self.output_file.text().strip(),
            self.percentile_selection.value(),
            self.low_percentile_selection.value()
        )


def get_layer(name):
    layers = QgsProject.instance().mapLayersByName(name)
    if not layers:
        raise ValueError(f"Layer '{name}' not found!")
    return layers[0]



class HighGround:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.plugin_dir = os.path.dirname(__file__)
        self.icon_path = os.path.join(self.plugin_dir, 'icon.png')

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

        vector_layers = [layer.name() for layer in layers if layer.type() == QgsMapLayer.VectorLayer]
        raster_layers = [layer.name() for layer in layers if layer.type() == QgsMapLayer.RasterLayer]

        if not vector_layers:
            QMessageBox.warning(self.iface.mainWindow(), "HighGround", "No vector layers loaded.")
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
            vector_layer, raster_layer, new_layer_name, perc_val, low_perc_val = dialog.get_selected_values()

            tasky = ProcTask('HighGround Processing', raster_layer, vector_layer, new_layer_name, perc_val, low_perc_val)
            QgsApplication.taskManager().addTask(tasky)
  

            QgsMessageLog.logMessage('Done', 'HighGround')
