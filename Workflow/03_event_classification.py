import sys
import os
from pathlib import Path
from datetime import datetime
import platform
import yaml
import git
import logging
import configargparse
import tarrow
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Sampler, RandomSampler
import numpy as np

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Add tarrow package path dynamically
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent
sys.path.insert(0, str(project_root / "TAP" / "tarrow"))
sys.path.append(str(project_root / "TAP" / "tarrow" / "tarrow"))


def save_config_metadata(args, output_dir):
    import collections.abc

    def is_basic_type(val):
        return isinstance(val, (str, int, float, bool, type(None))) or (
            isinstance(val, collections.abc.Sequence) and all(is_basic_type(v) for v in val)
        )

    metadata = {
        "timestamp": str(datetime.now().isoformat()),
        "python_version": str(platform.python_version()),
        "torch_version": str(torch.__version__),
        "cuda_available": str(torch.cuda.is_available()),
    }
    try:
        repo = git.Repo(search_parent_directories=True)
        metadata["git_commit"] = str(repo.head.object.hexsha)
    except Exception:
        metadata["git_commit"] = "unknown"

    config_path = Path(output_dir) / "training_config.yaml"
    # Only dump arguments with "basic" types
    filtered_args = {k: v for k, v in vars(args).items() if is_basic_type(v)}
    # Also convert all filtered_args values to str just to be extra safe
    filtered_args = {k: str(v) for k, v in filtered_args.items()}
    with open(config_path, "w") as f:
        yaml.safe_dump({**filtered_args, **metadata}, f)
    print(f"Saved config to {config_path}")


class BasicBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=stride, padding=1)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1)
        self.bn2 = nn.BatchNorm2d(out_channels)

        # If in_channels != out_channels or stride != 1, use a shortcut projection
        if stride != 1 or in_channels != out_channels:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=stride),
                nn.BatchNorm2d(out_channels)
            )
        else:
            self.shortcut = nn.Identity()  # Identity function

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        out = F.relu(out)
        return out

class SimpleResNet(nn.Module):
    def __init__(self, input_shape, num_cls):
        super(SimpleResNet, self).__init__()

        # Unpacking input shape
        batch_size, time_step, channel, height, width = input_shape

        # Initial conv layer takes 32 channels as input
        self.conv1 = nn.Conv2d(channel, 64, kernel_size=3, stride=1, padding=1)
        self.bn1 = nn.BatchNorm2d(64)

        # One ResNet block, using 64 channels for both input and output to ensure identity connection
        self.block = BasicBlock(64, 64, stride=1)  # No downsampling, identity connection ensured

        # Calculate the flattened size for the FC layer
        self.flattened_size = batch_size*time_step * 32 * height * width  # No downsampling, spatial dimensions unchanged

        # Fully connected layer for classification
        self.fc = nn.Linear(self.flattened_size, num_cls)

    def forward(self, x):
        # Merge the time_step into the batch dimension to handle 2D convolutions
        batch_size, time_step, channel, height, width = x.shape
        x = x.view(batch_size * time_step, channel, height, width)

        # Apply initial convolution and ResNet block
        x = F.relu(self.bn1(self.conv1(x)))
        x = self.block(x)

        # Reshape for FC layer
        x = x.view(batch_size, time_step, -1)
        x = x.mean(dim=1)  # Temporal mean pooling
        # print(f"Shape after mean pooling: {x.shape}")

        # Classification
        x = self.fc(x)
        return x



def plot_images_gray_scale(image1=None, image2=None, mask1=None, mask2=None):
    """
    plot image1 and image2 side by side for inspections
    :param image1: torch tensor
    :param image2: torch tensor
    :return:
    """
    import matplotlib.pyplot as plt
    if image1 is None:
        image1 = torch.zeros((1,96,96), dtype=torch.uint8)
    if image2 is None:
        image2 = torch.zeros((1,96,96), dtype=torch.uint8)
    if mask1 is None:
        mask1 = torch.zeros((1,96,96), dtype=torch.uint8)
    if mask2 is None:
        mask2 = torch.zeros((1,96,96), dtype=torch.uint8)

    image1_np = image1.squeeze().numpy()
    image2_np = image2.squeeze().numpy()
    mask1_np = mask1.squeeze().numpy()
    mask2_np = mask2.squeeze().numpy()

    fig, axes = plt.subplots(1, 4, figsize=(10, 5))

    axes[0].imshow(image1_np, cmap='gray')
    axes[0].axis('off')  # Turn off axis labels

    axes[1].imshow(image2_np, cmap='gray')
    axes[1].axis('off')

    axes[2].imshow(mask1_np, cmap='gray')
    axes[2].axis('off')

    axes[3].imshow(mask2_np, cmap='gray')
    axes[3].axis('off')

    # Show the plot
    plt.show()


class ClsHead(nn.Module):
    """
    classification head that takes the dense representation from the TAP model as input,
    output raw scores for each class of interest: (nothing of interest, cell division, cell death)
    architecture: fully connected layer [TBD]
    """
    def __init__(self, input_shape, num_cls):
        super(ClsHead, self).__init__()

        batch_size, time_step, channel, height, width = input_shape
        # input shape (Batch, Time, Channel, X, Y)
        self.flattened_size = time_step*channel*height*width

        # using a fully connected layer
        self.fc = nn.Linear(self.flattened_size, num_cls)

    def forward(self, x):
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x


# class ClsHead(nn.Module):
#     """
#     time-invariant head
#     Classification head that takes the dense representation from the TAP model as input,
#     and outputs raw scores for each class of interest: (nothing of interest, cell division, cell death).
#     The fully connected layer shares the same weights across the time_step dimension.
#     """
#     def __init__(self, input_shape, num_cls):
#         super(ClsHead, self).__init__()
#
#         batch_size, time_step, channel, height, width = input_shape
#         # input shape (Batch, Time, Channel, X, Y)
#
#         # Create a Conv3d layer with a kernel size of 1 in the time dimension
#         # This makes the weights shared across time steps.
#         self.conv = nn.Conv3d(
#             in_channels=channel,
#             out_channels=num_cls,
#             kernel_size=(2, height, width),  # 1 in time_step, full in spatial dimensions
#             stride=(1, 1, 1),
#             padding=(0, 0, 0)
#         )
#
#     def forward(self, x):
#         # x shape: (Batch, Time, Channel, X, Y)
#         x = x.permute(0, 2, 1, 3, 4)
#         # Apply convolution which shares weights across time steps
#         x = self.conv(x)  # output shape: (Batch, Time, num_cls, 1, 1)
#
#         # # Remove the unnecessary dimensions
#         # x = x.squeeze(-1).squeeze(-1)  # output shape: (Batch, Time, num_cls)
#         #
#         # # Optionally, you can average over the time dimension if you want a single output per batch
#         # x = x.mean(dim=1)  # output shape: (Batch, num_cls)
#
#         x = x.view(x.size(0), -1)
#
#         return x


def reinitialize_weights(model):
    import torch.nn.init as init
    for name, layer in model.named_modules():
        if hasattr(layer, 'weight') and layer.weight is not None:
            # Only reinitialize if the weight has 2 or more dimensions
            if len(layer.weight.shape) >= 2:
                init.kaiming_uniform_(layer.weight, nonlinearity='relu')
            else:
                # Handle 1D or fewer dimensions (e.g., BatchNorm, LayerNorm, etc.)
                init.normal_(layer.weight, mean=0.0, std=1.0)
        if hasattr(layer, 'bias') and layer.bias is not None:
            # Reinitialize bias (if it exists)
            init.zeros_(layer.bias)


def train_cls_head(train_loader, test_loader, patch_size, num_epochs, random_seed, device,
                   model_load_dir, cls_head_arch, TAP_init,
                   load_saved_cls_head=False, cls_head_load_path=None):
    """
    Train the classification head to the task of predicting cell event: no event, division, death
    for the input pair of patches.
    """
    import torch.nn as nn
    import torch.optim as optim
    from sklearn.metrics import confusion_matrix, classification_report
    import pandas as pd

    model = tarrow.models.TimeArrowNet.from_folder(model_folder=model_load_dir)
    model.to(device)

    if TAP_init == 'km_uniform':
        torch.manual_seed(random_seed)
        torch.cuda.manual_seed_all(random_seed)
        reinitialize_weights(model)
        print(f'- - - Initialising TAP model using {TAP_init} - - - ')
    elif TAP_init == 'loaded':
        print('- - - Initialising TAP model using loaded weights - - - ')

    for parem in model.parameters():
        parem.requires_grad = False

    # shape of the dense representation from the pretrained U-net is '(1, 2, 32, patch_size, patch_size)'
    # fix the random seed for reproducibility
    torch.manual_seed(random_seed)
    torch.cuda.manual_seed_all(random_seed)

    if cls_head_arch == 'linear':
        cls_head = ClsHead(input_shape=(1, 2, 32, patch_size, patch_size), num_cls=2).to(device)
    elif cls_head_arch == 'resnet':
        cls_head = SimpleResNet(input_shape=(1, 2, 32, patch_size, patch_size), num_cls=2).to(device)
    elif cls_head_arch == 'minimal':
        # For now, treat 'minimal' as 'linear'
        cls_head = ClsHead(input_shape=(1, 2, 32, patch_size, patch_size), num_cls=2).to(device)
    else:
        raise ValueError(f"Unknown cls_head_arch: {cls_head_arch} (expected 'linear', 'resnet', or 'minimal')")

    if load_saved_cls_head:
        print(f" - - Loading pretrained cls head - - ")
        cls_head_state_dict = torch.load(cls_head_load_path, map_location=device)
        cls_head.load_state_dict(cls_head_state_dict)

    optimizer = optim.Adam(cls_head.parameters(), lr=0.001)
    criterion = nn.CrossEntropyLoss()

    for epoch in range(num_epochs):
        cls_head.train()
        running_loss = 0.0
        correct = 0
        total = 0
        for datapoint in train_loader:
            x, y = datapoint[0].to(device), datapoint[1].to(device)
            # Forward pass through the pre-trained model to get the dense representation
            with torch.no_grad():  # Ensure the pre-trained model is not being updated
                rep = model.embedding(x)

            # Forward pass through the classification head
            outputs = cls_head(rep)

            # Calculate the loss
            loss = criterion(outputs, y)

            # Backward pass and optimization
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            running_loss += loss.item() * x.size(0)
            _, predicted = torch.max(outputs, 1)
            correct += (predicted == y).sum().item()
            total += y.size(0)

        epoch_loss = running_loss / total
        epoch_accuracy = correct / total
        print(f"Epoch [{epoch+1}/{num_epochs}], Loss: {epoch_loss:.4f}, Accuracy: {epoch_accuracy:.4f}")

    # test time
    running_loss = 0.0
    correct = 0
    total = 0
    count_event_interest = 0
    y_pred = []
    y_true = []
    cls_head.eval()
    with torch.no_grad():
        for datapoint in test_loader:
            x, y = datapoint[0].to(device), datapoint[1].to(device)
            rep = model.embedding(x)
            outputs = cls_head(rep)
            loss = criterion(outputs, y)

            running_loss += loss.item() * x.size(0)
            _, predicted = torch.max(outputs, 1)
            correct += (predicted == y).sum().item()
            total += y.size(0)
            count_event_interest += (y == 1).sum().item()
            y_pred.extend([t.item() for t in predicted])
            y_true.extend([t.item() for t in y])

    epoch_loss = running_loss / total
    epoch_accuracy = correct / total
    print(f"Test Loss: {epoch_loss:.4f}, Test accuracy: {epoch_accuracy:.4f}")
    print(f"There are {count_event_interest} out of {total} crops containing events of interest in the test set")

    cm_test = confusion_matrix(y_true, y_pred)
    cm_df = pd.DataFrame(cm_test, index=['Actual 0', 'Actual 1'], columns=['Predicted 0', 'Predicted 1'])
    print("Confusion Matrix test data:")
    print(cm_df)

    print(classification_report(y_true, y_pred, target_names=['class 0', 'class 1']))

    # computing the distribution of positive labels in the training set
    count_event_interest_train = 0
    total = 0
    y_pred = []
    y_true = []
    with torch.no_grad():
        for datapoint in train_loader:
            x, y = datapoint[0].to(device), datapoint[1].to(device)
            count_event_interest_train += (y == 1).sum().item()
            total += y.size(0)

            rep = model.embedding(x)
            outputs = cls_head(rep)
            _, predicted = torch.max(outputs, 1)
            y_pred.extend([t.item() for t in predicted])
            y_true.extend([t.item() for t in y])

    print(f"There are {count_event_interest_train} out of {total} crops containing events of interest in the training set")
    cm = confusion_matrix(y_true, y_pred)
    cm_df = pd.DataFrame(cm, index=['Actual 0', 'Actual 1'], columns=['Predicted 0', 'Predicted 1'])
    print("Confusion Matrix train data:")
    print(cm_df)
    print(classification_report(y_true, y_pred, target_names=['class 0', 'class 1']))

    return cls_head, model, cm_test
    

def count_data_points(dataloader):
    count = 0
    num_positive_event = 0
    for batch in dataloader:
        inputs, event_labels, labels = batch[0], batch[1], batch[2]
        count += inputs.size(0)  # Increment by the batch size
        num_positive_event += (event_labels == 1).sum().item()
    return count, num_positive_event


class BalancedSampler(Sampler):
    def __init__(self, data_source, num_crops_per_image, balanced_sample_size, data_gen_seed, sequential=False):
        self.data_source = data_source
        self.sequential = sequential
        self.num_crops_per_image = num_crops_per_image
        self.balanced_sample_size = balanced_sample_size  # the number of samples after resampling and balancing
        self.data_gen_seed = data_gen_seed
        num_image_pairs = len(self.data_source)
        # Separate the initial samples by label
        self.positive_indices = []
        self.negative_indices = []

        for i in range(num_image_pairs):
            if data_source[i][1] > 0:
                self.positive_indices.append(i)
            if data_source[i][1] == 0:
                self.negative_indices.append(i)

        # Ensure equal sampling from both classes
        self.num_samples_per_class = min(self.balanced_sample_size // 2, len(self.positive_indices),
                                         len(self.negative_indices))

    def get_combined_samples(self, data_gen_seed):
        import random
        # for reproducibility
        torch.manual_seed(data_gen_seed)

        if self.sequential:
            positive_samples = self.positive_indices[:self.num_samples_per_class]
            negative_samples = self.negative_indices[:self.num_samples_per_class]
        else:
            positive_samples = torch.multinomial(
                torch.ones(len(self.positive_indices)),
                self.num_samples_per_class,
                replacement=True
            ).tolist()
            positive_samples = [self.positive_indices[i] for i in positive_samples]

            negative_samples = torch.multinomial(
                torch.ones(len(self.negative_indices)),
                self.num_samples_per_class,
                replacement=True
            ).tolist()
            negative_samples = [self.negative_indices[i] for i in negative_samples]

        # Combine positive and negative samples
        # print(f"positive_samples : {len(positive_samples)}, negative_samples : {len(negative_samples)}")
        combined_samples = positive_samples + negative_samples

        # Shuffle the combined samples if not sequential
        if not self.sequential:
            # for reproducibility
            random.seed(data_gen_seed + 123)
            combined_samples = random.sample(combined_samples, len(combined_samples))
        return combined_samples

    def __iter__(self):
        combined_samples = self.get_combined_samples(data_gen_seed=self.data_gen_seed)
        return iter(combined_samples)

    def __len__(self):
        return 2 * self.num_samples_per_class


def probing_mistake_predictions(model, cls_head, test_data_loader, device):
    """
    Output mistake predictions according to the type (e.g. false positive).
    :param model:
    :param test_data:
    :param type_pred_err:
    :param num_outputs:
    :param test_data_loader: batchsize must be 1
    :return:
    """
    false_positives = []
    false_negatives = []
    logits_false_pos = []
    logits_false_neg = []
    cls_head.eval()
    with torch.no_grad():
        for datapoint in test_data_loader:
            x, y = datapoint[0].to(device), datapoint[1].to(device)
            # Forward pass through the pre-trained model to get the dense representation
            # Ensure the pre-trained model is not being updated
            rep = model.embedding(x)

            # Forward pass through the classification head
            outputs = cls_head(rep)
            _, predicted = torch.max(outputs, 1)
            datapoint.append(predicted.detach().cpu())
            # datapoint : (x_crop, event_label, label, crop_coordinates, predicted_value)
            # crop_coordinates = (torch.tensor(i), torch.tensor(j), torch.tensor(idx) (time index of the frame), TAP label)
            if (predicted == 1) and (y == 0):
                # false positive
                false_positives.append(datapoint)
                logits_false_pos.append(torch.squeeze(outputs.detach().cpu()))
            elif (predicted == 0) and (y == 1):
                false_negatives.append(datapoint)
                logits_false_neg.append(torch.squeeze(outputs.detach().cpu()))

    return false_positives, false_negatives, logits_false_pos, logits_false_neg


# def save_output_as_txt(data, output_f_path):
#     # Open a file to write the numerical data
#     with open(output_f_path, 'w') as f:
#         for item in data:
#             # Convert the tuple to a list of numbers
#             converted_item = []
#             for element in item:
#                 if isinstance(element, torch.Tensor):
#                     converted_item.append(element.item())  # Extract scalar value from tensor
#                 elif isinstance(element, list):  # Handle list of tensors
#                     converted_item.append([tensor.item() for tensor in element])
#
#             # Write the converted item to the file
#             f.write(str(converted_item) + '\n')
#
#     print(f"Successfully saved to {output_f_path}")


def probing_mistaken_preds(model, cls_head_trained, test_loader_probing, device):
    (false_positives, false_negatives,
     logits_false_pos, logits_false_neg) = probing_mistake_predictions(model,
                                                                       cls_head_trained,
                                                                       test_loader_probing,
                                                                       device)
    false_positives_coordinates = [tuple(e[1:]) for e in false_positives]
    false_negatives_coordinates = [tuple(e[1:]) for e in false_negatives]
    print(f"number of false_positives predictions: {len(false_positives_coordinates)}\n"
          f"number of false_negatives predictions: {len(false_negatives_coordinates)}")
    return (false_positives_coordinates, false_negatives_coordinates,
            false_positives, false_negatives, logits_false_pos, logits_false_neg)


def estimate_total_events(input_data):
    """
    estimate the total number of labels for events using a grid approach. The count is stored in data.
    so this function only aggregates them over the entire sequence of frames.
    :param data:
    :return: total count of event labels for the entire sequence of frames (a.k.a. movie)
    """
    total_count_events = 0
    for i in range(len(input_data)):
        count_current_image = input_data[i][1].detach().item()
        total_count_events += count_current_image
    return total_count_events


def save_as_json(input_data, file_save_path):
    import json
    data_to_save = []
    # Iterate over each item in the original list
    for item in input_data:
        converted_item = []
        # Iterate over each element in the current item
        for element in item:
            # Check if the element is a PyTorch tensor
            if hasattr(element, 'tolist'):
                # Convert the tensor to a list
                converted_item.append(element.tolist())
            else:
                # If it's not a tensor, keep it as is
                converted_item.append(element)

        # Append the converted item to the new list
        data_to_save.append(converted_item)
    # Saving the list as JSON
    with open(file_save_path, 'w') as f:
        json.dump(data_to_save, f)
    print(f"data saved to {file_save_path}")


def data_split(input_image_crops, train_data_ratio, validation_data_ratio, data_seed):
    """
    train, valid, test split. The test split will be determined by train_data_ratio and validation_data_ratio
    as it is given by the rest of the dataset after train and validation data are taken.
    :param input_image_crops:
    :param train_data_ratio:
    :param validation_data_ratio:
    :param data_seed:
    :return:
    """
    import random
    random.seed(data_seed)
    random.shuffle(input_image_crops)

    # Determine split indices
    total_length = len(input_image_crops)
    train_end = int(train_data_ratio * total_length)
    valid_end = train_end + int(validation_data_ratio * total_length)

    # Split the list
    train_data = input_image_crops[:train_end]
    valid_data = input_image_crops[train_end:valid_end]
    test_data = input_image_crops[valid_end:]

    # Verify the sizes
    print(f"Total data points: {total_length}")
    print(f"Training data points: {len(train_data)}")
    print(f"Validation data points: {len(valid_data)}")
    print(f"Test data points: {len(test_data)}")

    return train_data, valid_data, test_data


def multi_runs_training(num_runs, model_seed_init, train_loader, test_loader,
                        size, training_epochs, device, model_load_dir,
                        cls_head_arch,
                        TAP_init,
                        load_saved_cls_head=False,
                        cls_head_load_path=None):
    import numpy as np
    precision_class_0_all = []
    precision_class_1_all = []
    recall_class_0_all = []
    recall_class_1_all = []
    for i in range(num_runs):
        model_seed = model_seed_init + i*20 #args.model_seed
        cls_head_trained, model, cm_test = train_cls_head(cls_head_arch=cls_head_arch,
                                                          train_loader=train_loader,
                                                          test_loader=test_loader,
                                                          patch_size=size, # args.size
                                                          num_epochs=training_epochs, #args.training_epochs
                                                          random_seed=model_seed,
                                                          device=device,
                                                          model_load_dir=model_load_dir,
                                                          load_saved_cls_head=load_saved_cls_head,
                                                          cls_head_load_path=cls_head_load_path,
                                                          TAP_init=TAP_init)
        precision_class_0 = cm_test[0][0] / (cm_test[0][0] + cm_test[1][0])
        precision_class_1 = cm_test[1][1] / (cm_test[0][1] + cm_test[1][1])
        recall_class_0 = cm_test[0][0] / (cm_test[0][0] + cm_test[0][1])
        recall_class_1 = cm_test[1][1] / (cm_test[1][0] + cm_test[1][1])
        precision_class_0_all.append(precision_class_0)
        precision_class_1_all.append(precision_class_1)
        recall_class_0_all.append(recall_class_0)
        recall_class_1_all.append(recall_class_1)

    return (np.array(precision_class_0_all), np.array(precision_class_1_all),
            np.array(recall_class_0_all), np.array(recall_class_1_all),
            cls_head_trained, model)


def save_datasets(train_data_crops_flat, valid_data_crops_flat, test_data_crops_flat, dataset_save_dir):
    import os
    os.makedirs(dataset_save_dir, exist_ok=True)
    torch.save(train_data_crops_flat, os.path.join(dataset_save_dir, 'train_data_crops_flat.pth'))
    torch.save(valid_data_crops_flat, os.path.join(dataset_save_dir, 'valid_data_crops_flat.pth'))
    torch.save(test_data_crops_flat, os.path.join(dataset_save_dir, 'test_data_crops_flat.pth'))
    print(f"Train, validation and test data all saved to {dataset_save_dir}")


class CellEventClassModel(nn.Module):
    def __init__(self, TAPmodel, ClsHead):
        super(CellEventClassModel, self).__init__()
        self._TAPmodel = TAPmodel
        self._ClsHead = ClsHead

    def forward(self, _input):
        z = self._TAPmodel.embedding(_input)
        y = self._ClsHead(z)
        return y


import sys
import os
from pathlib import Path
from datetime import datetime
import platform
import yaml
import git
import logging
import configargparse
import tarrow
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Sampler, RandomSampler
import numpy as np

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Add tarrow package path dynamically
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent
sys.path.insert(0, str(project_root / "TAP" / "tarrow"))
sys.path.append(str(project_root / "TAP" / "tarrow" / "tarrow"))

# ... [ all functions/classes from your original script stay unchanged here ] ...

# (Omitted for brevity in this snippet: all the class/function definitions you pasted above,
# including: BasicBlock, SimpleResNet, plot_images_gray_scale, ClsHead, reinitialize_weights, 
# train_cls_head, count_data_points, BalancedSampler, probing_mistake_predictions, 
# probing_mistaken_preds, estimate_total_events, save_as_json, data_split, 
# multi_runs_training, save_datasets, CellEventClassModel)

def main():
    import time

    p = configargparse.ArgParser(
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        config_file_parser_class=configargparse.YAMLConfigFileParser,
        allow_abbrev=False,
    )

    p.add('--config', is_config_file=True, help='Path to config YAML file')
    p.add_argument("--input_frame")
    p.add_argument("--input_mask")
    p.add_argument("--cam_size", type=int, default=None)
    p.add_argument("--frames", type=int, default=2)
    p.add_argument("--n_images")
    p.add_argument("--subsample", type=int, default=1)
    p.add_argument("--binarize")
    p.add_argument("--timestamp")
    p.add_argument("--backbone", default='unet')
    p.add_argument("--name")
    p.add_argument("--size", type=int, default=96, required=True)
    p.add_argument("--ndim", type=int, default=2)
    p.add_argument("--batchsize", type=int, default=108)
    p.add_argument("--cam_subsampling", type=int, default=1)
    p.add_argument("--training_epochs", type=int, required=True)
    p.add_argument("--binary_problem", type=bool, default=True)
    p.add_argument("--balanced_sample_size", required=True, type=int)
    p.add_argument("--crops_per_image", required=True, type=int)
    p.add_argument("--model_seed", required=True, type=int)
    p.add_argument("--data_seed", required=True, type=int)
    p.add_argument("--data_save_dir", required=True)
    p.add_argument("--num_runs", type=int, required=True)
    p.add_argument("--model_save_dir", required=True)
    p.add_argument("--model_id", required=True)
    p.add_argument("--load_saved_cls_head", type=bool, default=False)
    p.add_argument("--cls_head_load_path", default=None)
    p.add_argument("--dataset_save_dir", default="runs", help="Directory to save datasets")
    p.add_argument("--TAP_model_load_path")
    p.add_argument("--cls_head_arch")
    p.add_argument("--TAP_init")

    args = p.parse_args()

    # Defensive: If dataset_save_dir is None or empty, use model_save_dir or "runs"
    if not args.dataset_save_dir:
        args.dataset_save_dir = args.model_save_dir or "runs"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("Running on", device)

    data_load_path = os.path.join(args.data_save_dir, 'preprocessed_image_crops.pth')
    image_crops_flat_loaded = torch.load(data_load_path)

    print(f"image_crops_flat_loaded: {len(image_crops_flat_loaded)}")

    train_data_ratio = 0.6
    validation_data_ratio = 0.2

    train_data_crops_flat, valid_data_crops_flat, test_data_crops_flat = data_split(
        image_crops_flat_loaded,
        train_data_ratio,
        validation_data_ratio,
        args.data_seed
    )

    # PATCH: Handle small datasets (≤2 crops)
    min_required_crops = 2  # allow pipeline to run for 1 or 2 crops
    if len(image_crops_flat_loaded) <= min_required_crops:
        print(f"⚠️  Very small dataset ({len(image_crops_flat_loaded)} crops).")
        print("   Using all crops for both training and testing. Splitting skipped.")
        train_data_crops_flat = image_crops_flat_loaded
        valid_data_crops_flat = []
        test_data_crops_flat = image_crops_flat_loaded

    # Extra check: gracefully exit if still not enough data
    if len(train_data_crops_flat) == 0 or len(test_data_crops_flat) == 0:
        print("\n❌ Not enough data to split into train/test sets!")
        print(f"Train size: {len(train_data_crops_flat)}, Test size: {len(test_data_crops_flat)}")
        print("Please check your input data or use a larger dataset.")
        return

    estimated_total_event_count = estimate_total_events(image_crops_flat_loaded)

    train_loader = DataLoader(
        train_data_crops_flat,
        sampler=BalancedSampler(
            train_data_crops_flat,
            args.crops_per_image,
            args.balanced_sample_size,
            data_gen_seed=args.data_seed,
            sequential=False
        ),
        batch_size=args.batchsize,
        num_workers=0,
        drop_last=False,
        persistent_workers=False
    )

    torch.manual_seed(args.data_seed)
    test_loader = DataLoader(
        test_data_crops_flat,
        sampler=RandomSampler(test_data_crops_flat),
        batch_size=args.batchsize,
        num_workers=0,
        drop_last=False,
        persistent_workers=False
    )

    print(f"Estimated event count: {estimated_total_event_count}")
    print(f"Train samples and positives: {count_data_points(train_loader)}")
    print(f"Test samples and positives: {count_data_points(test_loader)}")

    print(f"Loading pretrained TAP model from: {args.TAP_model_load_path}")

    test_loader_probing = DataLoader(
        test_data_crops_flat,
        batch_size=1,
        num_workers=0,
        drop_last=False,
        persistent_workers=False
    )

    start_time = time.time()

    (precision_class_0_all, precision_class_1_all,
     recall_class_0_all, recall_class_1_all,
     cls_head_trained, model) = multi_runs_training(
        args.num_runs, args.model_seed, train_loader,
        test_loader, args.size, args.training_epochs,
        device, args.TAP_model_load_path,
        cls_head_arch=args.cls_head_arch,
        TAP_init=args.TAP_init,
        load_saved_cls_head=args.load_saved_cls_head,
        cls_head_load_path=args.cls_head_load_path)

    print("Final Metrics (mean, std):")
    for label, values in zip(
        ["Precision Class 0", "Precision Class 1", "Recall Class 0", "Recall Class 1"],
        [precision_class_0_all, precision_class_1_all, recall_class_0_all, recall_class_1_all]
    ):
        print(f"{label}: mean={round(np.mean(values), 2)}, std={round(np.std(values), 2)}")

    end_time = time.time()
    print(f"Time used for model fine-tuning: {round(end_time - start_time, 2)} seconds")

    combined_model = CellEventClassModel(TAPmodel=model, ClsHead=cls_head_trained)
    os.makedirs(args.model_save_dir, exist_ok=True)
    model_save_path = os.path.join(args.model_save_dir, f'{args.model_id}.pth')
    torch.save(combined_model.state_dict(), model_save_path)
    print(f"Combined model saved to {model_save_path}")

    save_config_metadata(args, args.model_save_dir)

    save_datasets(train_data_crops_flat, valid_data_crops_flat,
                  test_data_crops_flat, args.data_save_dir)


if __name__ == "__main__":
    main()
