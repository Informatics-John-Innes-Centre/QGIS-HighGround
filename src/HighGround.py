import os

import numpy as np
import processing
from PyQt5.QtGui import QIcon
from osgeo import gdal
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QComboBox, QLineEdit
from qgis.PyQt.QtWidgets import QFileDialog, QDialog, QVBoxLayout, QLabel, QSpinBox, QPushButton
from qgis.core import QgsProject, QgsMapLayer, QgsTask, QgsMessageLog
from qgis.core import (QgsVectorLayer, QgsFeature, QgsField,
                       QgsCoordinateTransform, QgsVectorFileWriter, QgsGeometry)


class LayerSelectionDialog(QDialog):

    def __init__(self, vector_layers, raster_layers, parent=None):
        super().__init__(parent)

        self.save_as = None

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

        self.percentile_label = QLabel('Select percentile:')
        self.layout.addWidget(self.percentile_label)
        # Use QSpinBox for integer input between 1 and 99
        self.percentile_selection = QSpinBox()
        self.percentile_selection.setValue(99)
        self.percentile_selection.setRange(1, 99)  # Set the range from 1 to 99
        self.layout.addWidget(self.percentile_selection)

        self.output_label = QLabel('Choose Output Location:')
        self.layout.addWidget(self.output_label)
        self.output_file = QLineEdit()
        self.output_file.setPlaceholderText("Path to output file..")
        self.layout.addWidget(self.output_file)
        self.save_as_button = QPushButton('...')
        self.layout.addWidget(self.save_as_button)
        self.save_as_button.clicked.connect(self.output_file_dialog)

        proc_button = QPushButton("Process")
        proc_button.clicked.connect(self.accept)
        self.layout.addWidget(proc_button)

        self.setLayout(self.layout)

    def output_file_dialog(self):
        self.save_as, _ = QFileDialog.getSaveFileName(self, "Save File", "", "All Files(*);;Text Files(*.txt)")
        self.output_file.setText(self.save_as)

    def get_selected_values(self):
        return (
            self.vector_combo.currentText(),
            self.raster_combo.currentText(),
            self.output_file.text().strip(),
            self.percentile_selection.value()
        )


def get_layer(name):
    layers = QgsProject.instance().mapLayersByName(name)
    if not layers:
        raise ValueError(f"Layer '{name}' not found!")
    return layers[0]


def proc(raster, vector, output, selected_percentile):
    QgsMessageLog.logMessage("Begin processing", 'HighGround')

    try:
        raster_layer = get_layer(raster)
        vector_layer = get_layer(vector)
        original_crs = vector_layer.crs()

        # Create output layer with proper CRS
        output_layer = QgsVectorLayer(f"Polygon?crs={original_crs.authid()}", "temp_results", "memory")
        output_data = output_layer.dataProvider()

        # Add fields (original + new)
        new_fields = vector_layer.fields().toList() + [QgsField(f"pct_{selected_percentile}", QVariant.Double)]
        output_data.addAttributes(new_fields)
        output_layer.updateFields()

        # CRS transformation setup
        raster_crs = raster_layer.crs()
        transform_context = QgsProject.instance().transformContext()

        # Processing loop
        for idx, feature in enumerate(vector_layer.getFeatures()):
            original_geom = feature.geometry()
            attributes = feature.attributes()

            # Create transformed geometry for raster processing
            processing_geom = QgsGeometry(original_geom)
            if original_crs != raster_crs:
                xform = QgsCoordinateTransform(original_crs, raster_crs, transform_context)
                processing_geom.transform(xform)

            # Create temporary layer for clipping
            temp_vector = QgsVectorLayer(f"Polygon?crs={raster_crs.authid()}", "temp", "memory")
            temp_data = temp_vector.dataProvider()
            temp_feature = QgsFeature()
            temp_feature.setGeometry(processing_geom)
            temp_data.addFeatures([temp_feature])
            temp_vector.updateExtents()

            percentile = None
            try:
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

                # Calculate percentile
                ds = gdal.Open(clipped)
                band = ds.GetRasterBand(1)
                data = band.ReadAsArray()
                if data is not None:
                    data_flat = data.flatten()
                    nodata = band.GetNoDataValue()
                    if nodata is not None:
                        data_flat = data_flat[data_flat != nodata]
                    if data_flat.size > 0:
                        percentile = np.percentile(data_flat, selected_percentile)

            except Exception as e:
                QgsMessageLog.logMessage(f"Error processing feature {idx}: {str(e)}", 'HighGround')

            # Create output feature with ORIGINAL geometry
            new_feature = QgsFeature(output_layer.fields())
            new_feature.setGeometry(original_geom)  # Use original untransformed geometry
            new_feature.setAttributes(attributes + [percentile])
            output_data.addFeature(new_feature)

            QgsMessageLog.logMessage(f"Processed {idx + 1}/{vector_layer.featureCount()}", 'HighGround')

        # Save to shapefile
        save_options = QgsVectorFileWriter.SaveVectorOptions()
        save_options.driverName = "ESRI Shapefile"
        save_options.fileEncoding = "UTF-8"

        writer = QgsVectorFileWriter.writeAsVectorFormatV2(
            output_layer,
            output,
            transform_context,
            save_options
        )

        if writer[0] == QgsVectorFileWriter.NoError:
            # Load the result and zoom to it
            result_layer = QgsVectorLayer(output, "Percentile Results", "ogr")
            QgsProject.instance().addMapLayer(result_layer)
            result_layer.triggerRepaint()
            QgsMessageLog.logMessage(f"Success! Layer loaded at: {output}", 'HighGround')
        else:
            QgsMessageLog.logMessage("Failed to save output file!", 'HighGround')

    except Exception as e:
        QgsMessageLog.logMessage(f"Error: {str(e)}", 'HighGround')


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
        if dialog.exec_() == QDialog.Accepted:
            vector_layer, raster_layer, new_layer_name, perc_val = dialog.get_selected_values()

            if not new_layer_name:
                QMessageBox.warning(self.iface.mainWindow(), "HighGround", "Layer name cannot be empty.")
                return

            proc(raster_layer, vector_layer, new_layer_name, perc_val)
            QgsMessageLog.logMessage('Done', 'HighGround')
