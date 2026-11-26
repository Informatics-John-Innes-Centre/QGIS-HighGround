all: package-dev

package-dev:
	@set -eu; \
	tmp_dir=$$(mktemp -d); \
	trap 'rm -rf "$$tmp_dir"' EXIT; \
	output_zip="$(CURDIR)/QGIS-HighGround.zip"; \
	mkdir -p "$$tmp_dir/HighGround"; \
	cp -R src/. "$$tmp_dir/HighGround/"; \
	rm -f "$$output_zip"; \
	cd "$$tmp_dir" && zip -r "$$output_zip" HighGround

package-release:
	@set -eu; \
	version=$$(grep '^version=' src/metadata.txt | cut -d '=' -f2); \
	tmp_dir=$$(mktemp -d); \
	trap 'rm -rf "$$tmp_dir"' EXIT; \
	output_zip="$(CURDIR)/QGIS-HighGround-$$version.zip"; \
	mkdir -p "$$tmp_dir/HighGround"; \
	cp -R src/. "$$tmp_dir/HighGround/"; \
	rm -f "$$output_zip"; \
	cd "$$tmp_dir" && zip -r "$$output_zip" HighGround

clean:
	rm QGIS-HighGround.zip
