// Copyright (c) 2017-2018, NVIDIA CORPORATION. All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <iostream>
#include <sstream>

#include "dali/image/image.h"

namespace dali {

std::string ListSupportedExtensions() {
  std::stringstream ss;
  size_t known_extensions_count = sizeof(kKnownImageExtensions) / sizeof(*kKnownImageExtensions);
  for (size_t i = 0; i < known_extensions_count; i++) {
    ss << kKnownImageExtensions[i];
    if (i != known_extensions_count - 1) {
      ss << ", ";
    }
  }
  return ss.str();
}

bool HasKnownImageExtension(const std::string &image_path) {
  std::string path_low{image_path};
  std::transform(path_low.begin(), path_low.end(), path_low.begin(), ::tolower);

  /** Skip but without any warning */
  for (const auto &ext : kSkipImageExtensions) {
    if (strlen(ext) == strlen(image_path.c_str()) &&
        strncmp(ext, image_path.c_str(), strlen(ext)) == 0) {
      return false;
    }
  }

  for (const auto &ext : kKnownImageExtensions) {
    size_t pos = path_low.rfind(ext);
    if (pos != std::string::npos && pos + strlen(ext) == path_low.size()) {
      return true;
    }
  }
  std::cerr << "[Warning]: File " + image_path +
                   " has extension that is not supproted by the decoder. " +
                   "Supported extensions: " + ListSupportedExtensions() + "."
            << std::endl;
  return false;
}

Image::Image(const uint8_t *encoded_buffer, size_t length, DALIImageType image_type) :
        encoded_image_(encoded_buffer),
        length_(length),
        image_type_(image_type) {
}

void Image::Decode() {
  DALI_ENFORCE(!decoded_, "Called decode for already decoded image");
  auto decoded = DecodeImpl(image_type_, encoded_image_, length_);
  decoded_image_ = decoded.first;
  shape_ = decoded.second;
  decoded_ = true;
}


std::shared_ptr<uint8_t> Image::GetImage() const {
  DALI_ENFORCE(decoded_, "Image not decoded. Run Decode()");
  return decoded_image_;
}

Image::Shape Image::PeekShape() const {
  return PeekShapeImpl(encoded_image_, length_);
}

Image::Shape Image::GetShape() const {
  DALI_ENFORCE(decoded_, "Image not decoded. Run Decode()");
  return shape_;
}

}  // namespace dali
