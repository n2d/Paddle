/* Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License. */

#pragma once

#include "paddle/fluid/memory/malloc.h"
#include "paddle/phi/core/storage.h"

namespace paddle {
namespace experimental {

class ExternalStorage : public phi::Storage {
 public:
  ExternalStorage(void* ptr, size_t size, const phi::Place& place);
  ExternalStorage(const phi::intrusive_ptr<phi::Storage>& root,
                  size_t delta,
                  size_t size);

  static const char* name() { return "ExternalStorage"; }

  void Realloc(size_t n) override {
    PADDLE_THROW(phi::errors::Unavailable(
        "The external shared storage cannot be reallocated."));
  }

  void Clear() override {
    data_ = nullptr;
    size_ = 0;
  }

  void set_data_shared(
      const std::shared_ptr<paddle::memory::Allocation>& holder) override {
    CHECK(holder);
    data_ = holder;
    size_ = holder->size();
  }

  std::shared_ptr<paddle::memory::Allocation>&& move_data_shared() override {
    size_ = 0;
    return std::move(data_);
  }

  size_t size() const noexcept override { return size_; }
  const phi::Place& place() const override {
    PADDLE_ENFORCE_NOT_NULL(
        data_,
        phi::errors::Unavailable(
            "Unable to visit place as data_ has not been initialized yet."));
    return data_->place();
  }
  bool OwnsMemory() const noexcept override { return false; }

 private:
  int64_t size_{0};
};

class TensorStorage : public paddle::memory::allocation::Allocation {
 public:
  explicit TensorStorage(phi::intrusive_ptr<phi::Storage> storage)
      : paddle::memory::allocation::Allocation(
            storage->data(), storage->size(), storage->place()),
        storage_(std::move(storage)) {}

 private:
  phi::intrusive_ptr<phi::Storage> storage_;
};

}  // namespace experimental
}  // namespace paddle
