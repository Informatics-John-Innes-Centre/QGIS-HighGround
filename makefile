all: package

package-dev:
	zip -r QGIS-HighGround.zip src

package-release:
	zip -r QGIS-HighGround-$$(grep 'version' src/metadata.txt | cut -d '=' -f2).zip src

clean:
	rm QGIS-HighGround.zip
