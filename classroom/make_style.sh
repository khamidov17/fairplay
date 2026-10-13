#!/bin/sh
if ! [ -x "$(command -v sass)" ]; then
  echo 'Error: sass is not installed.' >&2
  exit 1
fi

if ! [ -x "$(command -v postcss)" ]; then
  echo 'Error: postcss is not installed.' >&2
  exit 1
fi

if ! [ -x "$(command -v autoprefixer)" ]; then
  echo 'Error: autoprefixer is not installed.' >&2
  exit 1
fi

cd "$(dirname "$0")" || exit

build_style() {
  echo "Creating $1 style..."
  cp resources/vars-$1.scss resources/vars.scss
  sass resources:sass_processed
  # Source of truth is *.scss. These postcss outputs overwrite tracked CSS under
  # resources/ (and resources/dark/). Edit the .scss files, then re-run this script.
  postcss \
      sass_processed/ace-dmoj.css \
      sass_processed/featherlight.css \
      sass_processed/martor-description.css \
      sass_processed/select2-dmoj.css \
      sass_processed/style.css \
      sass_processed/problem_create_v2.css \
      sass_processed/rich_editor.css \
      --verbose --use autoprefixer -d "$2"
  rm resources/vars.scss
}

build_style 'default' 'resources'
build_style 'dark' 'resources/dark'
