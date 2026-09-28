#!/bin/sh
# Download the bundled-font set into <project>/fonts with each licence beside it.
# Every face here is SIL OFL 1.1: free to bundle and to render into commercial video.
# Usage: sh fetch_fonts.sh <project-dir> [zh] [en]
#   zh: wenkai (default) | xiaolai | kuaile
#   en: comicneue (default) | patrickhand | shortstack | architectsdaughter | caveat
set -e
dir=${1:?project dir}; zh=${2:-wenkai}; en=${3:-comicneue}
mkdir -p "$dir/fonts"; cd "$dir/fonts"
gf=https://raw.githubusercontent.com/google/fonts/main/ofl
get() { curl -sSfL -o "$1" "$2"; echo "  $1"; }
case $zh in
  wenkai)  get LXGWWenKai-Medium.ttf https://github.com/lxgw/LxgwWenKai/releases/download/v1.522/LXGWWenKai-Medium.ttf
           get OFL-LXGWWenKai.txt https://raw.githubusercontent.com/lxgw/LxgwWenKai/v1.522/OFL.txt ;;
  xiaolai) get Xiaolai-Regular.ttf https://github.com/lxgw/kose-font/releases/download/v3.126/Xiaolai-Regular.ttf
           get OFL-Xiaolai.txt https://raw.githubusercontent.com/lxgw/kose-font/main/OFL.txt ;;
  kuaile)  get ZCOOLKuaiLe-Regular.ttf $gf/zcoolkuaile/ZCOOLKuaiLe-Regular.ttf
           get OFL-ZCOOLKuaiLe.txt $gf/zcoolkuaile/OFL.txt ;;
  none) ;;
  *) echo "unknown zh font $zh"; exit 1 ;;
esac
case $en in
  comicneue) get ComicNeue-Regular.ttf $gf/comicneue/ComicNeue-Regular.ttf; get ComicNeue-Bold.ttf $gf/comicneue/ComicNeue-Bold.ttf; get OFL-ComicNeue.txt $gf/comicneue/OFL.txt ;;
  patrickhand) get PatrickHand-Regular.ttf $gf/patrickhand/PatrickHand-Regular.ttf; get OFL-PatrickHand.txt $gf/patrickhand/OFL.txt ;;
  shortstack) get ShortStack-Regular.ttf $gf/shortstack/ShortStack-Regular.ttf; get OFL-ShortStack.txt $gf/shortstack/OFL.txt ;;
  architectsdaughter) get ArchitectsDaughter-Regular.ttf $gf/architectsdaughter/ArchitectsDaughter-Regular.ttf; get OFL-ArchitectsDaughter.txt $gf/architectsdaughter/OFL.txt ;;
  caveat) get Caveat-Variable.ttf "$gf/caveat/Caveat%5Bwght%5D.ttf"; get OFL-Caveat.txt $gf/caveat/OFL.txt ;;
  none) ;;
  *) echo "unknown en font $en"; exit 1 ;;
esac
echo "fonts ready in $dir/fonts"
